//! CI 重放用：把对象目录当成"远端"，只凭交付清单把数据根装出来。
//!
//! 对象目录按清单里的 `key` 平铺（`<objects>/<key>`），和将来 HTTPS 桶里的键
//! 一一对应；所以这个例子跑通 = 清单 → 合同落点 → 逐文件校验这条消费路径跑通。
//! 它不联网、不起引擎；起引擎由车道在装好之后单独做。

use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;

use local_ai_studio_core::delivery::{install, Delivery};
use local_ai_studio_core::CoreError;

struct Args {
    manifest: PathBuf,
    objects: PathBuf,
    data: PathBuf,
    os: String,
}

fn usage() -> String {
    "--manifest <delivery-manifest.json> --objects <dir> --data <dir> --os <platform>".into()
}

fn parse() -> Result<Args, String> {
    let mut manifest = None;
    let mut objects = None;
    let mut data = None;
    let mut os = None;
    let mut it = env::args().skip(1);
    while let Some(flag) = it.next() {
        let value = it.next().ok_or_else(|| format!("{flag} 后面缺值"))?;
        match flag.as_str() {
            "--manifest" => manifest = Some(PathBuf::from(value)),
            "--objects" => objects = Some(PathBuf::from(value)),
            "--data" => data = Some(PathBuf::from(value)),
            "--os" => os = Some(value),
            other => return Err(format!("不认识的参数 {other}")),
        }
    }
    Ok(Args {
        manifest: manifest.ok_or_else(usage)?,
        objects: objects.ok_or_else(usage)?,
        data: data.ok_or_else(usage)?,
        os: os.ok_or_else(usage)?,
    })
}

fn run(args: &Args) -> Result<(), CoreError> {
    let delivery = Delivery::from_file(&args.manifest)?;
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
