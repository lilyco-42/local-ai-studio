use std::fmt;

#[derive(Debug)]
pub enum CoreError {
    Io(String),
    Json(String),
    Contract(String),
    Platform(String),
    Manifest(String),
    Spawn(String),
    Protocol(String),
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoreError::Io(m) => write!(f, "IO: {m}"),
            CoreError::Json(m) => write!(f, "JSON: {m}"),
            CoreError::Contract(m) => write!(f, "合同: {m}"),
            CoreError::Platform(m) => write!(f, "平台: {m}"),
            CoreError::Manifest(m) => write!(f, "载荷: {m}"),
            CoreError::Spawn(m) => write!(f, "启动: {m}"),
            CoreError::Protocol(m) => write!(f, "协议: {m}"),
        }
    }
}

impl std::error::Error for CoreError {}

impl From<std::io::Error> for CoreError {
    fn from(e: std::io::Error) -> Self {
        CoreError::Io(e.to_string())
    }
}

impl From<serde_json::Error> for CoreError {
    fn from(e: serde_json::Error) -> Self {
        CoreError::Json(e.to_string())
    }
}
