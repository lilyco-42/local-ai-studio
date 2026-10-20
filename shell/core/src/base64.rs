use crate::error::CoreError;

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn value(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// 只认标准字母表；空白跳过，其他字符一律报错（不做 URL-safe 猜测）。
pub fn decode(text: &str) -> Result<Vec<u8>, CoreError> {
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let mut acc: u32 = 0;
    let mut bits = 0u32;
    let mut padding = 0usize;
    let mut seen = 0usize;
    for (idx, byte) in text.bytes().enumerate() {
        if byte.is_ascii_whitespace() {
            continue;
        }
        seen += 1;
        if byte == b'=' {
            padding += 1;
            continue;
        }
        if padding > 0 {
            return Err(CoreError::Protocol(format!(
                "base64 第 {idx} 个字符在补位后又出现了数据"
            )));
        }
        let v = value(byte).ok_or_else(|| {
            CoreError::Protocol(format!("base64 第 {idx} 个字符不是标准字母表里的: {byte:#x}"))
        })?;
        acc = (acc << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    if seen % 4 != 0 {
        return Err(CoreError::Protocol(format!(
            "base64 长度 {seen} 不是 4 的倍数"
        )));
    }
    if padding > 2 {
        return Err(CoreError::Protocol(format!("base64 补了 {padding} 个 =")));
    }
    if bits >= 6 {
        return Err(CoreError::Protocol("base64 末尾多出整字节".into()));
    }
    Ok(out)
}

pub fn encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc4648_vectors() {
        assert_eq!(encode(b""), "");
        assert_eq!(encode(b"f"), "Zg==");
        assert_eq!(encode(b"fo"), "Zm8=");
        assert_eq!(encode(b"foo"), "Zm9v");
        assert_eq!(encode(b"foob"), "Zm9vYg==");
        assert_eq!(encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(encode(b"foobar"), "Zm9vYmFy");
        for v in [b"".as_slice(), b"f", b"fo", b"foo", b"foob", b"fooba", b"foobar"] {
            assert_eq!(decode(&encode(v)).unwrap(), v);
        }
    }

    #[test]
    fn newlines_in_a_json_string_are_tolerated() {
        assert_eq!(decode("Zm9v\r\nYmFy").unwrap(), b"foobar");
    }

    #[test]
    fn garbage_is_refused_instead_of_guessed() {
        assert!(decode("Zm9v-_==").is_err());
        assert!(decode("Zm9").is_err());
        assert!(decode("Z===").is_err());
        assert!(decode("Zg==Zg==").is_err());
    }

    #[test]
    fn round_trip_of_binary_bytes() {
        let bytes: Vec<u8> = (0..=255u8).collect();
        assert_eq!(decode(&encode(&bytes)).unwrap(), bytes);
    }
}
