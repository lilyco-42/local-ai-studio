//! 首启取负载：按交付清单里的对象键从一个或多个基址取回文件，校验后才交给 `delivery::install`。
//!
//! 与 `download.rs` 的分工：那边管权重（扁平文件名、内容寻址的 blobs），这里管交付对象
//! （键是 `<prefix>/<dest>`，要保留目录层级）。字节仍然由同一个 `download::Fetcher`
//! （系统 curl）取，所以 TLS、低速放弃、断流都沿用那一层的参数。
//!
//! 盘上顺序：对象目录里先攒 `<key>.part` → 长度与 sha256 都对 → 改名成 `<key>`。
//! 中途断掉时 `.part` 留着，下一次从它的长度续传；哈希不符则丢掉重下。
//!
//! 基址只收 https；`http://127.0.0.1` 与 `http://localhost` 是给 CI 重放用的例外
//! （不放进生产配置，单元测试会证明公网 http 被拒）。

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::download::{Fetcher, Policy};
use crate::error::CoreError;
use crate::hex_encode;
use crate::manifest::sha256_file;

/// 校验过的基址列表，顺序就是换源顺序。
#[derive(Debug, Clone)]
pub struct Bases {
    list: Vec<String>,
}

impl Bases {
    pub fn parse(raw: &[String]) -> Result<Self, CoreError> {
        if raw.is_empty() {
            return Err(CoreError::Download("一个下载基址都没有".into()));
        }
        let mut list = Vec::with_capacity(raw.len());
        for base in raw {
            check_base(base)?;
            let trimmed = base.trim_end_matches('/');
            if !list.iter().any(|b| b == trimmed) {
                list.push(trimmed.to_string());
            }
        }
        Ok(Self { list })
    }

    pub fn urls_for(&self, key: &str) -> Vec<String> {
        let encoded = encode_key(key);
        self.list
            .iter()
            .map(|base| format!("{base}/{encoded}"))
            .collect()
    }
}

/// 取一个对象：先看对象目录里有没有现成的，没有才联网。返回的字节已经与清单对过。
pub fn fetch_object(
    bases: &Bases,
    key: &str,
    bytes: u64,
    sha256: &str,
    objects: &Path,
    fetcher: &mut dyn Fetcher,
    policy: Policy,
) -> Result<Vec<u8>, CoreError> {
    check_key(key)?;
    let sha = checked_sha(key, sha256)?;
    if bytes == 0 {
        return Err(CoreError::Download(format!("{key}: 清单写的字节数是 0")));
    }
    if policy.attempts_per_source == 0 {
        return Err(CoreError::Download("每个基址的尝试次数不能是 0".into()));
    }

    let target = objects.join(key);
    if good(&target, bytes, &sha)? {
        return read_all(&target);
    }
    fs::create_dir_all(objects)?;
    let part = with_suffix(&target, ".part");
    let chunk = with_suffix(&target, ".chunk");

    let mut notes: Vec<String> = Vec::new();
    for url in bases.urls_for(key) {
        for attempt in 1..=policy.attempts_per_source {
            let _ = fs::remove_file(&chunk);
            let mut from = len_of(&part).unwrap_or(0);
            if from > bytes {
                let _ = fs::remove_file(&part);
                from = 0;
            }
            if from == bytes {
                if good(&part, bytes, &sha)? {
                    land(&part, &target)?;
                    return read_all(&target);
                }
                let _ = fs::remove_file(&part);
                from = 0;
            }

            let reply = match fetcher.fetch_range(&url, from, &chunk) {
                Ok(reply) => reply,
                Err(e) => {
                    push_note(&mut notes, format!("{url} 第 {attempt} 次: {e}"));
                    continue;
                }
            };

            match reply.status {
                200 => {
                    let _ = fs::remove_file(&part);
                    if let Err(e) = fs::rename(&chunk, &part) {
                        push_note(&mut notes, format!("{url}: 收下响应失败: {e}"));
                        continue;
                    }
                }
                206 => {
                    if from == 0 || reply.range_start == Some(0) {
                        let _ = fs::remove_file(&part);
                        if let Err(e) = fs::rename(&chunk, &part) {
                            push_note(&mut notes, format!("{url}: 收下响应失败: {e}"));
                            continue;
                        }
                    } else if reply.range_start == Some(from) {
                        if let Err(e) = append(&chunk, &part) {
                            push_note(&mut notes, format!("{url}: 续传拼接失败: {e}"));
                            continue;
                        }
                        let _ = fs::remove_file(&chunk);
                    } else {
                        push_note(
                            &mut notes,
                            format!(
                                "{url}: 要的是 {from} 之后，服务端给的起点是 {:?}",
                                reply.range_start
                            ),
                        );
                        continue;
                    }
                }
                416 => {
                    if from != bytes {
                        push_note(&mut notes, format!("{url}: 416 但本地只有 {from}/{bytes} 字节"));
                        continue;
                    }
                }
                other => {
                    push_note(&mut notes, format!("{url}: HTTP {other}"));
                    continue;
                }
            }

            match length_and_sha(&part, bytes, &sha)? {
                Part::Good => {
                    land(&part, &target)?;
                    return read_all(&target);
                }
                Part::Short(got) => {
                    // 断流但响应头是 ok 的：.part 留着，下一轮从 got 续。
                    push_note(&mut notes, format!("{url}: 只取到 {got}/{bytes} 字节"));
                }
                Part::Corrupt(got) => {
                    let _ = fs::remove_file(&part);
                    push_note(
                        &mut notes,
                        format!("{url}: sha256 不符（{got} 字节，重下）"),
                    );
                }
                Part::TooLong(got) => {
                    let _ = fs::remove_file(&part);
                    push_note(&mut notes, format!("{url}: 取回 {got} 字节，超过 {bytes}"));
                }
            }
        }
    }
    Err(CoreError::Download(format!(
        "{key}: 没用任何基址取到可校验的字节（{}）",
        notes.join("；")
    )))
}

fn push_note(notes: &mut Vec<String>, note: String) {
    if notes.len() < 8 {
        notes.push(note);
    } else if notes.len() == 8 {
        notes.push("…".into());
    }
}

enum Part {
    Good,
    Short(u64),
    Corrupt(u64),
    TooLong(u64),
}

fn length_and_sha(path: &Path, bytes: u64, sha: &str) -> Result<Part, CoreError> {
    let got = match fs::metadata(path) {
        Ok(m) if m.is_file() => m.len(),
        Ok(_) => return Err(CoreError::Download(format!("{} 不是普通文件", path.display()))),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Part::Short(0)),
        Err(e) => return Err(e.into()),
    };
    if got > bytes {
        return Ok(Part::TooLong(got));
    }
    if got < bytes {
        return Ok(Part::Short(got));
    }
    let actual = sha256_file(path)?;
    if actual == sha {
        Ok(Part::Good)
    } else {
        Ok(Part::Corrupt(got))
    }
}

fn good(path: &Path, bytes: u64, sha: &str) -> Result<bool, CoreError> {
    match length_and_sha(path, bytes, sha)? {
        Part::Good => Ok(true),
        Part::Short(_) => Ok(false),
        Part::Corrupt(_) | Part::TooLong(_) => Ok(false),
    }
}

fn land(part: &Path, target: &Path) -> Result<(), CoreError> {
    if target.exists() {
        fs::remove_file(target)?;
    }
    fs::rename(part, target)
        .map_err(|e| CoreError::Download(format!("把 {} 落位失败: {e}", target.display())))
}

fn read_all(path: &Path) -> Result<Vec<u8>, CoreError> {
    fs::read(path).map_err(|e| CoreError::Download(format!("读 {} 失败: {e}", path.display())))
}

fn with_suffix(target: &Path, suffix: &str) -> PathBuf {
    let mut s = target.as_os_str().to_os_string();
    s.push(suffix);
    PathBuf::from(s)
}

fn len_of(path: &Path) -> Option<u64> {
    fs::metadata(path)
        .ok()
        .filter(|m| m.is_file())
        .map(|m| m.len())
}

fn append(chunk: &Path, part: &Path) -> Result<(), CoreError> {
    let mut src = fs::File::open(chunk)?;
    let mut dst = fs::OpenOptions::new().create(true).append(true).open(part)?;
    std::io::copy(&mut src, &mut dst)?;
    dst.flush()?;
    Ok(())
}

fn checked_sha(key: &str, sha: &str) -> Result<String, CoreError> {
    if sha.len() != 64 || !sha.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(CoreError::Download(format!(
            "{key}: sha256 {sha:?} 不是 64 位十六进制"
        )));
    }
    Ok(sha.to_ascii_lowercase())
}

fn check_key(key: &str) -> Result<(), CoreError> {
    if key.is_empty()
        || key.contains('\\')
        || key.contains(':')
        || key.contains('\0')
        || key.starts_with('/')
        || key.ends_with('/')
    {
        return Err(CoreError::Download(format!("对象键 {key:?} 不合法")));
    }
    for part in key.split('/') {
        if part.is_empty() || part == "." || part == ".." {
            return Err(CoreError::Download(format!(
                "对象键 {key:?} 含不安全的段 {part:?}"
            )));
        }
    }
    Ok(())
}

fn check_base(base: &str) -> Result<(), CoreError> {
    let b = base.trim_end_matches('/');
    if b.is_empty() || b.contains('?') || b.contains('#') {
        return Err(CoreError::Download(format!("下载基址 {base:?} 不合法")));
    }
    if let Some(rest) = b.strip_prefix("https://") {
        if rest.is_empty() {
            return Err(CoreError::Download(format!("下载基址 {base:?} 没有主机名")));
        }
        return Ok(());
    }
    if let Some(rest) = b.strip_prefix("http://") {
        let host_port = rest.split('/').next().unwrap_or("");
        let host = host_port.split(':').next().unwrap_or("");
        if host == "127.0.0.1" || host == "localhost" {
            return Ok(());
        }
        return Err(CoreError::Download(format!(
            "下载基址 {base:?} 不是 https（只有 127.0.0.1/localhost 允许 http，给重放用）"
        )));
    }
    Err(CoreError::Download(format!(
        "下载基址 {base:?} 不是 https:// 开头"
    )))
}

fn encode_key(key: &str) -> String {
    let mut out = String::with_capacity(key.len());
    for &b in key.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' | b'/' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::download::FetchReply;
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let p = std::env::temp_dir().join(format!(
                "lasfetch-{}-{}-{}",
                std::process::id(),
                tag,
                N.fetch_add(1, Ordering::SeqCst)
            ));
            fs::create_dir_all(&p).unwrap();
            Self(p)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn digest(body: &[u8]) -> String {
        hex_encode(Sha256::digest(body).as_slice())
    }

    struct Fake {
        bodies: BTreeMap<String, Vec<u8>>,
        bad: Vec<String>,
        calls: Vec<(String, u64)>,
        honor_range: bool,
        truncate_once: Option<usize>,
    }

    impl Fake {
        fn new(bodies: &[(&str, &[u8])]) -> Self {
            Self {
                bodies: bodies
                    .iter()
                    .map(|(u, b)| (u.to_string(), b.to_vec()))
                    .collect(),
                bad: Vec::new(),
                calls: Vec::new(),
                honor_range: true,
                truncate_once: None,
            }
        }

        fn corrupt(mut self, url: &str) -> Self {
            self.bad.push(url.to_string());
            self
        }

        fn ignore_range(mut self) -> Self {
            self.honor_range = false;
            self
        }

        fn truncate_first(mut self, n: usize) -> Self {
            self.truncate_once = Some(n);
            self
        }
    }

    impl Fetcher for Fake {
        fn fetch_range(
            &mut self,
            url: &str,
            from: u64,
            dest: &Path,
        ) -> Result<FetchReply, CoreError> {
            self.calls.push((url.to_string(), from));
            let mut body = self
                .bodies
                .get(url)
                .cloned()
                .ok_or_else(|| CoreError::Download(format!("fake 不认识 {url}")))?;
            if self.bad.iter().any(|b| b == url) {
                if let Some(first) = body.first_mut() {
                    *first ^= 0xff;
                }
            }
            let (start, status) = if from > 0 && self.honor_range {
                if from as usize > body.len() {
                    fs::write(dest, b"")?;
                    return Ok(FetchReply {
                        status: 416,
                        range_start: None,
                        total: Some(body.len() as u64),
                        bytes: 0,
                    });
                }
                (from as usize, 206)
            } else {
                (0, 200)
            };
            let full = body.len() as u64;
            let mut tail = body.split_off(start);
            if let Some(n) = self.truncate_once.take() {
                tail.truncate(n);
            }
            fs::write(dest, &tail)?;
            Ok(FetchReply {
                status,
                range_start: if status == 206 { Some(start as u64) } else { None },
                total: Some(full),
                bytes: tail.len() as u64,
            })
        }
    }

    fn bases(list: &[&str]) -> Bases {
        Bases::parse(&list.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap()
    }

    #[test]
    fn an_object_is_fetched_verified_and_placed_by_key() {
        let d = TempDir::new("ok");
        let body: &[u8] = b"engine payload";
        let bases = bases(&["https://mirror.example/payload"]);
        let mut fake = Fake::new(&[("https://mirror.example/payload/payload/linux-x64/a.bin", body)]);
        let got = fetch_object(
            &bases,
            "payload/linux-x64/a.bin",
            body.len() as u64,
            &digest(body),
            d.path(),
            &mut fake,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(got, body);
        assert_eq!(fs::read(d.path().join("payload/linux-x64/a.bin")).unwrap(), body);
        assert!(!d.path().join("payload/linux-x64/a.bin.part").exists());
        assert_eq!(fake.calls.len(), 1);
        assert_eq!(fake.calls[0].1, 0);
    }

    #[test]
    fn a_second_call_reads_the_object_without_the_network() {
        let d = TempDir::new("idem");
        let body: &[u8] = b"cached";
        let bases = bases(&["https://m.example"]);
        let url = "https://m.example/payload/linux-x64/a.bin";
        let mut fake = Fake::new(&[(url, body)]);
        let sha = digest(body);
        fetch_object(&bases, "payload/linux-x64/a.bin", 6, &sha, d.path(), &mut fake, Policy::default())
            .unwrap();
        let mut second = Fake::new(&[]);
        let got = fetch_object(
            &bases,
            "payload/linux-x64/a.bin",
            6,
            &sha,
            d.path(),
            &mut second,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(got, body);
        assert!(second.calls.is_empty());
    }

    #[test]
    fn a_corrupt_mirror_falls_through_to_the_next_base() {
        let d = TempDir::new("failover");
        let body: &[u8] = b"real bytes";
        let key = "payload/linux-x64/a.bin";
        let bad_url = "https://bad.example/payload/linux-x64/a.bin";
        let good_url = "https://good.example/payload/linux-x64/a.bin";
        let mut fake = Fake::new(&[(bad_url, body), (good_url, body)]).corrupt(bad_url);
        let got = fetch_object(
            &bases(&["https://bad.example", "https://good.example"]),
            key,
            body.len() as u64,
            &digest(body),
            d.path(),
            &mut fake,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(got, body);
        assert_eq!(fake.calls.first().unwrap().0, bad_url);
        assert!(fake.calls.iter().any(|(u, _)| u == good_url));
    }

    #[test]
    fn every_corrupt_base_leaves_no_object_and_says_why() {
        let d = TempDir::new("allbad");
        let body: &[u8] = b"real bytes";
        let key = "payload/linux-x64/a.bin";
        let url = "https://bad.example/payload/linux-x64/a.bin";
        let mut fake = Fake::new(&[(url, body)]).corrupt(url);
        let err = fetch_object(
            &bases(&["https://bad.example"]),
            key,
            body.len() as u64,
            &digest(body),
            d.path(),
            &mut fake,
            Policy::default(),
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("sha256"), "{err}");
        assert!(!d.path().join(key).exists());
        assert!(!d.path().join(format!("{key}.part")).exists());
    }

    #[test]
    fn a_partial_part_resumes_with_a_range_request() {
        let d = TempDir::new("resume");
        let body: &[u8] = b"0123456789abcdef";
        let key = "payload/linux-x64/a.bin";
        let url = "https://m.example/payload/linux-x64/a.bin";
        let part = d.path().join(format!("{key}.part"));
        fs::create_dir_all(part.parent().unwrap()).unwrap();
        fs::write(&part, &body[..7]).unwrap();
        let mut fake = Fake::new(&[(url, body)]);
        let got = fetch_object(
            &bases(&["https://m.example"]),
            key,
            body.len() as u64,
            &digest(body),
            d.path(),
            &mut fake,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(got, body);
        assert_eq!(fake.calls[0].1, 7);
        assert!(!part.exists());
    }

    #[test]
    fn a_server_that_ignores_range_does_not_duplicate_the_prefix() {
        let d = TempDir::new("norange");
        let body: &[u8] = b"0123456789abcdef";
        let key = "payload/linux-x64/a.bin";
        let url = "https://m.example/payload/linux-x64/a.bin";
        let part = d.path().join(format!("{key}.part"));
        fs::create_dir_all(part.parent().unwrap()).unwrap();
        fs::write(&part, &body[..7]).unwrap();
        let mut fake = Fake::new(&[(url, body)]).ignore_range();
        let got = fetch_object(
            &bases(&["https://m.example"]),
            key,
            body.len() as u64,
            &digest(body),
            d.path(),
            &mut fake,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(got, body);
        assert_eq!(fake.calls[0].1, 7);
    }

    #[test]
    fn a_truncated_response_keeps_the_part_and_the_next_try_resumes() {
        let d = TempDir::new("trunc");
        let body: &[u8] = b"0123456789abcdef";
        let key = "payload/linux-x64/a.bin";
        let url = "https://m.example/payload/linux-x64/a.bin";
        let mut fake = Fake::new(&[(url, body)]).truncate_first(5);
        let got = fetch_object(
            &bases(&["https://m.example"]),
            key,
            body.len() as u64,
            &digest(body),
            d.path(),
            &mut fake,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(got, body);
        assert_eq!(fake.calls.len(), 2);
        assert_eq!(fake.calls[1].1, 5);
    }

    #[test]
    fn a_complete_part_is_verified_without_any_request() {
        let d = TempDir::new("complete");
        let body: &[u8] = b"already here";
        let key = "payload/linux-x64/a.bin";
        let part = d.path().join(format!("{key}.part"));
        fs::create_dir_all(part.parent().unwrap()).unwrap();
        fs::write(&part, body).unwrap();
        let mut fake = Fake::new(&[]);
        let got = fetch_object(
            &bases(&["https://m.example"]),
            key,
            body.len() as u64,
            &digest(body),
            d.path(),
            &mut fake,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(got, body);
        assert!(fake.calls.is_empty());
    }

    #[test]
    fn public_http_and_other_schemes_are_refused_at_parse_time() {
        for bad in ["http://example.com/x", "ftp://example.com/x", "file:///tmp/x", "https://"] {
            let err = Bases::parse(&[bad.to_string()]).unwrap_err().to_string();
            assert!(err.contains("基址"), "{bad}: {err}");
        }
        assert!(Bases::parse(&["http://127.0.0.1:8765".to_string()]).is_ok());
        assert!(Bases::parse(&["http://localhost:8765/prefix/".to_string()]).is_ok());
    }

    #[test]
    fn urls_join_one_slash_and_encode_unsafe_bytes() {
        let b = bases(&["https://m.example/base/"]);
        assert_eq!(
            b.urls_for("payload/linux-x64/a b.bin"),
            vec!["https://m.example/base/payload/linux-x64/a%20b.bin".to_string()]
        );
    }

    #[test]
    fn keys_that_escape_the_object_directory_are_refused() {
        for bad in ["../x", "payload/../x", "/abs", "payload//x", "a\\b"] {
            let err = fetch_object(
                &bases(&["https://m.example"]),
                bad,
                1,
                &"0".repeat(64),
                Path::new("."),
                &mut Fake::new(&[]),
                Policy::default(),
            )
            .unwrap_err()
            .to_string();
            assert!(err.contains("键"), "{bad}: {err}");
        }
    }
}
