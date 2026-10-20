//! 外壳核心：三条验收里可以在没有 Tauri、没有引擎的情况下被测试的部分。
//!
//! 这一层不监听任何端口（除了 `alloc_port` 里一次 127.0.0.1:0 的探测）；
//! 引擎的命令行、目录、旗标全部来自 `pins/shell-contract.json`（编译期嵌入），
//! 源码里不写死引擎名、旗标或端口。

pub mod base64;
pub mod contract;
pub mod error;
pub mod http;
pub mod manifest;
pub mod supervisor;

pub use contract::{Contract, CONTRACT_JSON};
pub use error::CoreError;
pub use supervisor::{LaunchPlan, OcrRequest, Status, Supervisor};

/// OS 随机数转 hex。字节数由合同里的 `auth.token_bytes` 决定，不在源码里写 32。
pub fn token_hex(n_bytes: usize) -> Result<String, CoreError> {
    if n_bytes == 0 {
        return Err(CoreError::Contract("token 字节数必须 > 0".into()));
    }
    let mut buf = vec![0u8; n_bytes];
    use rand::RngCore;
    rand::rngs::OsRng.fill_bytes(&mut buf);
    Ok(hex_encode(&buf))
}

pub fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{:02x}", *b));
    }
    out
}
