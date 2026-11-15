// Оболочка — только окно с веб-клиентом: вся логика в Vue, весь «мозг» — в ядре.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("не удалось запустить приложение");
}
