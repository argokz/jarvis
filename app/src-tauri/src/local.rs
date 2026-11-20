//! То, что оболочка узнаёт об этой машине без веб-страницы: настройки ядра из .env и службы systemd.
//! Только std — модуль проверяется отдельно: `rustc --test src/local.rs`.

/// Значение ключа из .env: `KEY=VALUE`, комментарий после « #», кавычки вокруг значения.
/// Как у python-dotenv, побеждает последнее определение; пустое значение — ключа нет.
pub fn env_value(text: &str, key: &str) -> Option<String> {
    let found = text.lines().filter_map(|line| {
        let line = line.trim();
        let (k, v) = line.strip_prefix("export ").unwrap_or(line).split_once('=')?;
        if k.trim() != key {
            return None;
        }
        let v = v.split(" #").next().unwrap_or("").trim();
        Some(v.trim_matches(|c| c == '"' || c == '\'').to_string())
    });
    found.last().filter(|v| !v.is_empty())
}

/// Адрес ядра: `JARVIS_APP_SERVER`, иначе — из адреса голосового канала, который там уже записан.
pub fn server(text: &str) -> String {
    if let Some(s) = env_value(text, "JARVIS_APP_SERVER") {
        return s.trim_end_matches('/').to_string();
    }
    env_value(text, "JARVIS_VOICE_URL")
        .and_then(|url| {
            let (scheme, rest) = url.split_once("://")?;
            let host = rest.split('/').next().filter(|h| !h.is_empty())?;
            Some(format!("{}://{}", if scheme == "wss" { "https" } else { "http" }, host))
        })
        .unwrap_or_else(|| "http://127.0.0.1:8000".to_string())
}

/// Ссылка, которую можно отдать системе на открытие: только веб-адрес, без пробелов и управляющих знаков.
pub fn is_web_url(url: &str) -> bool {
    let rest = url.strip_prefix("https://").or_else(|| url.strip_prefix("http://"));
    rest.is_some_and(|r| !r.is_empty() && !url.chars().any(|c| c.is_whitespace() || c.is_control()))
}

/// Службы ассистента: имя, название, пояснение и можно ли выключить совсем.
pub const UNITS: &[(&str, &str, &str, bool)] = &[
    ("jarvis-core", "Ядро", "Мозг ассистента: без него не работает ничего", false),
    ("jarvis-stt", "Распознавание речи", "Свой сервер распознавания", false),
    ("jarvis-tts", "Синтез речи", "Свой сервер голоса", false),
    ("jarvis-voice", "Слушать имя", "Микрофон этого компьютера: ассистент откликается на своё имя", true),
    ("jarvis-piper", "Запасной голос", "Piper — на случай, если основной голос недоступен", true),
    ("jarvis-clone", "Клон голоса", "Синтез по образцу на видеокарте: медленный, держит память", true),
];

#[derive(Debug, PartialEq)]
pub struct UnitState {
    pub unit: String,
    pub active: bool,
    pub enabled: bool,
}

/// Разбор `systemctl show -p Id,LoadState,ActiveState,UnitFileState`: блоки через пустую строку.
/// Службы, которых на машине нет (LoadState=not-found), пропускаются.
pub fn parse_units(text: &str) -> Vec<UnitState> {
    text.split("\n\n")
        .filter_map(|block| {
            let get = |key: &str| {
                block.lines().find_map(|l| l.strip_prefix(key).and_then(|r| r.strip_prefix('=')))
            };
            if get("LoadState")? != "loaded" {
                return None;
            }
            Some(UnitState {
                unit: get("Id")?.trim_end_matches(".service").to_string(),
                active: matches!(get("ActiveState")?, "active" | "activating" | "reloading"),
                enabled: get("UnitFileState").unwrap_or("") == "enabled",
            })
        })
        .collect()
}

/// Аргументы systemctl для действия из приложения; чужие службы и действия не пропускаются.
pub fn action_args(unit: &str, action: &str) -> Result<Vec<String>, String> {
    if !UNITS.iter().any(|u| u.0 == unit) {
        return Err(format!("неизвестная служба: {unit}"));
    }
    let head: &[&str] = match action {
        "start" | "stop" | "restart" => &[],
        "enable" | "disable" => &["--now"],
        _ => return Err(format!("неизвестное действие: {action}")),
    };
    let mut args = vec![action.to_string()];
    args.extend(head.iter().map(|s| s.to_string()));
    args.push(format!("{unit}.service"));
    Ok(args)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ENV: &str = "# ключи\nJARVIS_API_TOKEN=old\nexport JARVIS_API_TOKEN=\"abc123\"  # openssl rand -hex 32\n\
                       JARVIS_VOICE_URL=ws://127.0.0.1:8010/voice/ws\nNTFY_URL=      # пусто\nBROKEN LINE\n";

    #[test]
    fn reads_values_like_dotenv() {
        assert_eq!(env_value(ENV, "JARVIS_API_TOKEN").as_deref(), Some("abc123"));
        assert_eq!(env_value(ENV, "NTFY_URL"), None);
        assert_eq!(env_value(ENV, "MISSING"), None);
        assert_eq!(env_value("A='x y'\n", "A").as_deref(), Some("x y"));
    }

    #[test]
    fn finds_core_address() {
        assert_eq!(server(ENV), "http://127.0.0.1:8010");
        assert_eq!(server("JARVIS_VOICE_URL=wss://pc.ts.net/voice/ws"), "https://pc.ts.net");
        assert_eq!(server("JARVIS_APP_SERVER=http://10.0.0.2:9000/\nJARVIS_VOICE_URL=ws://x/voice/ws"), "http://10.0.0.2:9000");
        assert_eq!(server(""), "http://127.0.0.1:8000");
    }

    #[test]
    fn opens_only_web_addresses() {
        assert!(is_web_url("http://homeassistant.local:8123/config/integrations/dashboard"));
        assert!(is_web_url("https://ha.example/x?y=1"));
        for bad in ["file:///etc/passwd", "javascript:alert(1)", "https://", "--help", "http://a b", "http://a\nb", ""] {
            assert!(!is_web_url(bad), "{bad}");
        }
    }

    #[test]
    fn parses_systemctl_show() {
        let out = "Id=jarvis-core.service\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\n\n\
                   Id=jarvis-voice.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n\n\
                   Id=jarvis-clone.service\nLoadState=not-found\nActiveState=inactive\nUnitFileState=\n";
        assert_eq!(
            parse_units(out),
            vec![
                UnitState { unit: "jarvis-core".into(), active: true, enabled: true },
                UnitState { unit: "jarvis-voice".into(), active: false, enabled: false },
            ]
        );
    }

    #[test]
    fn allows_only_own_units_and_known_actions() {
        assert_eq!(action_args("jarvis-voice", "enable").unwrap(), ["enable", "--now", "jarvis-voice.service"]);
        assert_eq!(action_args("jarvis-core", "restart").unwrap(), ["restart", "jarvis-core.service"]);
        assert!(action_args("sshd", "stop").is_err());
        assert!(action_args("jarvis-core", "mask").is_err());
        assert!(action_args("jarvis-core --now x", "start").is_err());
    }
}
