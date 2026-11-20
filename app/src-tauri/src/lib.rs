//! Оболочка: окно с веб-клиентом. Логика клиента — в Vue, «мозг» — в ядре.
//! На ПК оболочка добавляет то, чего веб-странице нельзя: значок в трее (окно закрывается в него и
//! остаётся наготове), один экземпляр, автозапуск, настройки подключения с этой машины и её службы.
mod local;

use serde::Serialize;
use tauri::Manager;

/// Адрес ядра и токен для клиента.
#[derive(Serialize)]
struct Conn {
    server: String,
    token: String,
}

#[derive(Serialize)]
struct Service {
    unit: String,
    title: String,
    note: String,
    optional: bool,
    active: bool,
    enabled: bool,
}

/// Есть ли значок в трее: без него окно нельзя прятать — его будет нечем вернуть.
struct HasTray(bool);

/// Настройки подключения из .env ядра на этой машине: `JARVIS_ENV_FILE` или `<config>/jarvis/env`
/// (установщик кладёт туда ссылку на .env). Нет файла или токена — клиент спросит их сам.
#[tauri::command]
fn local_conn(app: tauri::AppHandle) -> Option<Conn> {
    let path = std::env::var_os("JARVIS_ENV_FILE")
        .map(std::path::PathBuf::from)
        .or_else(|| app.path().config_dir().ok().map(|d| d.join("jarvis").join("env")))?;
    let text = std::fs::read_to_string(path).ok()?;
    Some(Conn { server: local::server(&text), token: local::env_value(&text, "JARVIS_API_TOKEN")? })
}

#[cfg(target_os = "linux")]
fn systemctl(args: &[String]) -> Result<String, String> {
    let out = std::process::Command::new("systemctl")
        .arg("--user")
        .args(args)
        .output()
        .map_err(|e| format!("systemctl не запустился: {e}"))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

/// Службы ассистента на этой машине; там, где systemd нет, список пуст.
#[tauri::command(async)]
fn services() -> Vec<Service> {
    #[cfg(target_os = "linux")]
    {
        let mut args = vec!["show".to_string(), "--property=Id,LoadState,ActiveState,UnitFileState".to_string()];
        args.extend(local::UNITS.iter().map(|u| format!("{}.service", u.0)));
        let states = local::parse_units(&systemctl(&args).unwrap_or_default());
        local::UNITS
            .iter()
            .filter_map(|(unit, title, note, optional)| {
                let state = states.iter().find(|s| s.unit == *unit)?;
                Some(Service {
                    unit: unit.to_string(),
                    title: title.to_string(),
                    note: note.to_string(),
                    optional: *optional,
                    active: state.active,
                    enabled: state.enabled,
                })
            })
            .collect()
    }
    #[cfg(not(target_os = "linux"))]
    Vec::new()
}

/// start, stop, restart, enable, disable для своей службы; возвращает новое состояние всех служб.
#[tauri::command(async)]
fn service_action(unit: String, action: String) -> Result<Vec<Service>, String> {
    let args = local::action_args(&unit, &action)?;
    #[cfg(target_os = "linux")]
    {
        systemctl(&args)?;
        Ok(services())
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = args;
        Err("службами на этой системе приложение не управляет".to_string())
    }
}

fn reveal(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Клиент зовёт, когда нужен владелец: звенит будильник или ждёт подтверждение. Спрятанное окно
/// показываем, видимое не трогаем — только просим внимания, чтобы не отбирать фокус у другой работы.
#[tauri::command]
fn show_window(app: tauri::AppHandle) {
    let Some(window) = app.get_webview_window("main") else { return };
    if window.is_visible().unwrap_or(false) {
        let _ = window.request_user_attention(Some(tauri::UserAttentionType::Informational));
    } else {
        reveal(&app);
    }
}

#[cfg(desktop)]
fn tray(app: &tauri::App) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

    let open = MenuItem::with_id(app, "open", "Открыть Jarvis", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Выйти", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    let mut builder = TrayIconBuilder::with_id("main")
        .tooltip("Jarvis")
        .menu(&menu)
        .show_menu_on_left_click(false) // левый щелчок открывает окно (в Linux значок умеет только меню)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => reveal(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                reveal(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();
    #[cfg(desktop)]
    {
        builder = builder
            // второй запуск не открывает второе окно, а показывает первое — так работает и ярлык, и горячая клавиша
            .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| reveal(app)))
            .plugin(tauri_plugin_autostart::init(
                tauri_plugin_autostart::MacosLauncher::LaunchAgent,
                Some(vec!["--hidden"]),
            ))
            .on_window_event(|window, event| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    if window.app_handle().try_state::<HasTray>().is_some_and(|t| t.0) {
                        api.prevent_close(); // крестик прячет окно в трей; выход — в меню значка
                        let _ = window.hide();
                    }
                }
            });
    }
    builder
        .invoke_handler(tauri::generate_handler![local_conn, services, service_action, show_window])
        .setup(|app| {
            #[cfg(desktop)]
            {
                let args: Vec<String> = std::env::args().collect();
                let has_tray = match tray(app) {
                    Ok(()) => true,
                    Err(e) => {
                        eprintln!("значок в трее не создан: {e}");
                        false
                    }
                };
                app.manage(HasTray(has_tray));
                if args.iter().any(|a| a == "--autostart") {
                    use tauri_plugin_autostart::ManagerExt;
                    if let Err(e) = app.autolaunch().enable() {
                        eprintln!("автозапуск не включён: {e}");
                    }
                }
                // окно создаётся скрытым: при входе в систему (--hidden) оно остаётся в трее
                if !(has_tray && args.iter().any(|a| a == "--hidden")) {
                    reveal(app.handle());
                }
            }
            #[cfg(not(desktop))]
            let _ = app;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("не удалось запустить приложение");
}
