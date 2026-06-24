//! xkg-payments desktop — Tauri entrypoint.

mod payments;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            payments::list_products,
            payments::create_customer,
            payments::create_subscription,
            payments::cancel_subscription,
            payments::service_health,
        ])
        .run(tauri::generate_context!())
        .expect("error while running xkg-payments desktop");
}
