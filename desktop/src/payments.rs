//! xkg-payments desktop — Tauri commands.
//!
//! Thin Rust wrapper that talks to the xkg-payments FastAPI service over
//! HTTP. The Tauri shell UI calls these commands via `invoke()` and never
//! touches the network directly.

use serde::{Deserialize, Serialize};
use std::env;

const DEFAULT_SERVICE_URL: &str = "http://127.0.0.1:8765";
const DEFAULT_BEARER: &str = "devtoken-change-me";

fn service_url() -> String {
    env::var("XKG_PAYMENTS_URL").unwrap_or_else(|_| DEFAULT_SERVICE_URL.to_string())
}

fn bearer_token() -> String {
    env::var("XKG_PAYMENTS_TOKEN").unwrap_or_else(|_| DEFAULT_BEARER.to_string())
}

#[derive(Debug, thiserror::Error)]
pub enum PayError {
    #[error("HTTP error: {0}")]
    Http(String),
    #[error("Service error: {0}")]
    Service(String),
    #[error("Serialization error: {0}")]
    Serde(String),
}

impl serde::Serialize for PayError {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_string())
    }
}

pub type PayResult<T> = Result<T, PayError>;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Product {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub tier: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub prices: Vec<Price>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Price {
    pub id: String,
    pub currency: String,
    pub unit_amount: i64,
    #[serde(default)]
    pub interval: Option<String>,
    #[serde(default)]
    pub trial_period_days: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Customer {
    pub id: String,
    pub email: String,
    pub stripe_customer_id: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Subscription {
    pub id: String,
    pub status: String,
    pub current_period_end: String,
    pub cancel_at_period_end: bool,
    pub stripe_subscription_id: String,
}

async fn http_get<T: for<'de> Deserialize<'de>>(path: &str) -> PayResult<T> {
    let url = format!("{}{}", service_url(), path);
    let resp = reqwest::Client::new()
        .get(&url)
        .bearer_auth(bearer_token())
        .send()
        .await
        .map_err(|e| PayError::Http(e.to_string()))?;
    if !resp.status().is_success() {
        return Err(PayError::Service(format!(
            "GET {} returned {}",
            path,
            resp.status()
        )));
    }
    resp.json::<T>()
        .await
        .map_err(|e| PayError::Serde(e.to_string()))
}

async fn http_post<T: for<'de> Deserialize<'de>, B: Serialize>(
    path: &str,
    body: &B,
) -> PayResult<T> {
    let url = format!("{}{}", service_url(), path);
    let resp = reqwest::Client::new()
        .post(&url)
        .bearer_auth(bearer_token())
        .json(body)
        .send()
        .await
        .map_err(|e| PayError::Http(e.to_string()))?;
    if !resp.status().is_success() {
        return Err(PayError::Service(format!(
            "POST {} returned {}",
            path,
            resp.status()
        )));
    }
    resp.json::<T>()
        .await
        .map_err(|e| PayError::Serde(e.to_string()))
}

// ── Tauri commands (callable from JS via `invoke()`) ──────────────────────

#[tauri::command]
pub async fn list_products() -> PayResult<Vec<Product>> {
    #[derive(Deserialize)]
    struct Wrapper { data: Vec<Product> }
    let w: Wrapper = http_get("/v1/products").await?;
    Ok(w.data)
}

#[tauri::command]
pub async fn create_customer(email: String, name: Option<String>) -> PayResult<Customer> {
    #[derive(Serialize)]
    struct Body<'a> { email: &'a str, name: Option<&'a str> }
    http_post("/v1/customers", &Body { email: &email, name: name.as_deref() }).await
}

#[tauri::command]
pub async fn create_subscription(
    customer_id: String,
    price_id: String,
    trial_days: Option<i64>,
) -> PayResult<Subscription> {
    #[derive(Serialize)]
    struct Body { customer_id: String, price_id: String, trial_days: Option<i64> }
    http_post("/v1/subscriptions", &Body { customer_id, price_id, trial_days }).await
}

#[tauri::command]
pub async fn cancel_subscription(
    subscription_id: String,
    at_period_end: Option<bool>,
) -> PayResult<Subscription> {
    #[derive(Serialize)]
    struct Body { at_period_end: bool }
    http_post(
        &format!("/v1/subscriptions/{}/cancel", subscription_id),
        &Body { at_period_end: at_period_end.unwrap_or(true) },
    )
    .await
}

#[tauri::command]
pub async fn service_health() -> PayResult<String> {
    // /health is public, no bearer required
    let url = format!("{}/health", service_url());
    let resp = reqwest::Client::new()
        .get(&url)
        .send()
        .await
        .map_err(|e| PayError::Http(e.to_string()))?;
    if !resp.status().is_success() {
        return Err(PayError::Service(format!("/health returned {}", resp.status())));
    }
    resp.text().await.map_err(|e| PayError::Http(e.to_string()))
}
