// xkg-payments desktop — main binary.
// Prevents additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    xkg_payments_desktop::run();
}
