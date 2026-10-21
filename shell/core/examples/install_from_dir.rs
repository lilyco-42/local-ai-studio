//! CI 重放用：把对象目录当成"远端"，只凭交付清单把数据根装出来。
//!
//! 对象目录按清单里的 `key` 平铺（`<objects>/<key>`），和将来 HTTPS 桶里的键
//! 一一对应；所以这个例子跑通 = 清单 → 合同落点 → 逐文件校验这条消费路径跑通。
//!
//! `--stage-from <数据根>` 先按清单把一棵已经摆好的树复制成对象目录，再删掉那棵树；
//! 之后的安装只能读对象目录，证明没有偷看生产者。`--run-help` 从装好的树里找
//! `llama-server` 跑一次 `--help`：只看"装载器能解析依赖并打印用法"，所以 rc 收 0/1，
//! 但输出里必须有 `--port`。两个开关都只给 CI 用，生产路径仍是 `delivery::install`。

use std::env;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use local_ai_studio_core::delivery::{install, Delivery, DeliveryFile};
use local_ai_studio_core::CoreError;

struct Args {
    manifest: PathBuf,
    objects: PathBuf,
    data: PathBuf,
    os: String,
    stage_from: Option<PathBuf>,
    run_help: bool,
}

fn usage() -> String {
    "--manifest <delivery-manifest.json> --objects <dir> --data <dir> --os <platform> \
     [--stage-from <已摆好的数据根>] [--run-help]"
        .into()
}

fn parse() -> Result<Args, String> {
    let mut manifest = None;
    let mut objects = None;
    let mut data = None;
    let mut os = None;
    let mut stage_from = None;
    let mut run_help = false;
    let mut it = env::args().skip(1);
    while let Some(flag) = it.next() {
        if flag == "--run-help" {
            run_help = true;
            continue;
        }
        let value = it.next().ok_or_else(|| format!("{flag} 后面缺值"))?;
        match flag.as_str() {
            "--manifest" => manifest = Some(PathBuf::from(value)),
            "--objects" => objects = Some(PathBuf::from(value)),
            "--data" => data = Some(PathBuf::from(value)),
            "--os" => os = Some(value),
            "--stage-from" => stage_from = Some(PathBuf::from(value)),
            other => return Err(format!("不认识的参数 {other}")),
        }
    }
    Ok(Args {
        manifest: manifest.ok_or_else(usage)?,
        objects: objects.ok_or_else(usage)?,
        data: data.ok_or_else(usage)?,
        os: os.ok_or_else(usage)?,
        stage_from,
        run_help,
    })
}

fn build_objects(delivery: &Delivery, stage: &Path, objects: &Path) -> Result<usize, CoreError> {
    if objects.exists() {
        return Err(CoreError::Manifest(format!(
            "对象目录已存在，先删掉: {}",
            objects.display()
        )));
    }
    let mut rows: Vec<&DeliveryFile> = delivery.files.iter().collect();
    rows.push(&delivery.manifest);
    for row in &rows {
        let src = stage.join(&row.dest);
        let dst = objects.join(&row.key);
        if let Some(parent) = dst.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(&src, &dst).map_err(|e| {
            CoreError::Manifest(format!(
                "从摆好的树复制 {} 到 {} 失败: {e}",
                src.display(),
                dst.display()
            ))
        })?;
    }
    fs::remove_dir_all(stage).map_err(|e| {
        CoreError::Manifest(format!("删掉摆好的数据根 {} 失败: {e}", stage.display()))
    })?;
    Ok(rows.len())
}

fn find_binary(root: &Path, stem: &str) -> Option<PathBuf> {
    let entries = fs::read_dir(root).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if let Some(found) = find_binary(&path, stem) {
                return Some(found);
            }
        } else if path.file_stem().and_then(|s| s.to_str()) == Some(stem) {
            return Some(path);
        }
    }
    None
}

fn count_files(root: &Path) -> Result<usize, CoreError> {
    let mut n = 0;
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_dir() {
                stack.push(entry.path());
            } else if kind.is_file() {
                n += 1;
            }
        }
    }
    Ok(n)
}

fn make_executable(path: &Path) -> Result<(), CoreError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perm = fs::metadata(path)?.permissions();
        perm.set_mode(perm.mode() | 0o111);
        fs::set_permissions(path, perm)?;
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
    Ok(())
}

fn command_for(bin: &Path) -> Command {
    let mut cmd = Command::new(bin);
    let dir = bin.parent().unwrap_or_else(|| Path::new("."));
    let mut paths = vec![dir.to_path_buf()];
    if let Some(old) = env::var_os("PATH") {
        paths.extend(env::split_paths(&old));
    }
    if let Ok(joined) = env::join_paths(paths) {
        cmd.env("PATH", joined);
    }
    #[cfg(unix)]
    {
        cmd.env("LD_LIBRARY_PATH", dir);
        cmd.env("DYLD_LIBRARY_PATH", dir);
    }
    cmd
}

fn tail(text: &str, lines: usize) -> String {
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

fn run_help(bin: &Path) -> Result<(), CoreError> {
    make_executable(bin)?;
    let out_path = env::temp_dir().join(format!("lasd-help-{}.out", std::process::id()));
    let err_path = env::temp_dir().join(format!("lasd-help-{}.err", std::process::id()));
    let out = File::create(&out_path)?;
    let err = File::create(&err_path)?;
    let mut child = command_for(bin)
        .arg("--help")
        .stdout(Stdio::from(out))
        .stderr(Stdio::from(err))
        .spawn()
        .map_err(|e| CoreError::Spawn(format!("起不来 {}: {e}", bin.display())))?;
    let deadline = Instant::now() + Duration::from_secs(60);
    let code = loop {
        match child.try_wait()? {
            Some(status) => break status.code().unwrap_or(-1),
            None if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CoreError::Spawn("--help 60 秒内没结束".into()));
            }
            None => sleep(Duration::from_millis(100)),
        }
    };
    let mut text = String::new();
    for path in [&out_path, &err_path] {
        if let Ok(mut fh) = File::open(path) {
            let mut chunk = String::new();
            let _ = fh.read_to_string(&mut chunk);
            text.push_str(&chunk);
        }
    }
    let _ = fs::remove_file(&out_path);
    let _ = fs::remove_file(&err_path);
    println!("llama-server --help rc={code}");
    if !text.contains("--port") {
        return Err(CoreError::Spawn(format!(
            "--help 输出里没有 --port；尾部:\n{}",
            tail(&text, 30)
        )));
    }
    if code != 0 && code != 1 {
        return Err(CoreError::Spawn(format!(
            "rc={code} 不是 0/1（装载或启动失败）；尾部:\n{}",
            tail(&text, 30)
        )));
    }
    Ok(())
}

fn run(args: &Args) -> Result<(), CoreError> {
    let delivery = Delivery::from_file(&args.manifest)?;
    if let Some(stage) = &args.stage_from {
        let n = build_objects(&delivery, stage, &args.objects)?;
        println!("对象目录：{n} 个键（含根清单），生产者那棵树已删掉");
    }
    let objects = &args.objects;
    let report = install(&delivery, &args.os, &args.data, |key| {
        let path = objects.join(key);
        fs::read(&path).map_err(|e| {
            CoreError::Manifest(format!("对象 {} 读不出来: {e}", path.display()))
        })
    })?;
    let total: u64 = delivery.files.iter().map(|f| f.bytes).sum();
    println!(
        "{}: 装好 {} 个文件（跳过 {} 个已就位），{:.1} MiB；根清单在 {}",
        args.os,
        report.written,
        report.skipped,
        total as f64 / 1048576.0,
        args.data.join("payload-manifest.json").display()
    );
    if args.run_help {
        let runtime = args.data.join("runtime");
        let bin = find_binary(&runtime, "llama-server").ok_or_else(|| {
            CoreError::Manifest(format!("装好的树里没有 llama-server: {}", runtime.display()))
        })?;
        println!("== 从装好的树起：{} ==", bin.display());
        run_help(&bin)?;
        println!(
            "::notice::交付清单 → 空数据根 → llama-server --help 成功（{} 个文件，{:.1} MiB）",
            count_files(&args.data)?,
            total as f64 / 1048576.0
        );
    }
    Ok(())
}

fn main() -> ExitCode {
    let args = match parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("安装失败: {e}");
            ExitCode::FAILURE
        }
    }
}
