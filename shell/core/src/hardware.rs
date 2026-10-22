//! 硬件识别：只读系统信息，不改任何设置，也不起推理进程。
//!
//! 真正联网/起进程的动作不在这里：这里只跑几条只读探测命令（`nvidia-smi`、
//! `vulkaninfo`、`lspci`、PowerShell 的 CIM 查询），拿不到就当没有。
//! 所有判据都在 `classify(&Signals)` 里，测试注入夹具，不依赖跑测试的那台机器。

use std::fs;
use std::process::Command;

use serde::Serialize;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Cpu {
    pub arch: String,
    pub cores: usize,
    pub brand: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Accelerator {
    /// `gpu` 或 `npu`。
    pub kind: String,
    pub vendor: String,
    pub name: String,
    pub memory_mib: Option<u64>,
    /// `cuda` / `rocm` / `vulkan` / `metal` / `openvino` / `qnn-snapdragon` / `rknpu` / `ane`。
    pub backend: String,
    /// 上游是否有与这个后端对应的引擎产物；具体装没装由外壳的交付清单决定。
    pub usable: bool,
    pub note: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Backend {
    pub id: String,
    pub label: String,
    pub accelerated: bool,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Hardware {
    pub os: String,
    pub arch: String,
    pub cpu: Cpu,
    pub ram_mib: Option<u64>,
    pub accelerators: Vec<Accelerator>,
    pub backend: Backend,
}

/// 探测结果。测试只构造这个结构，不跑命令。
#[derive(Debug, Clone, Default)]
pub struct Signals {
    pub os: String,
    pub arch: String,
    pub cores: usize,
    pub cpu_brand: Option<String>,
    pub ram_bytes: Option<u64>,
    pub nvidia_smi: Option<String>,
    pub vulkaninfo: Option<String>,
    pub lspci: Option<String>,
    pub windows_gpus: Vec<String>,
    pub accel_devices: Vec<String>,
    pub apple_silicon: bool,
}

fn vendor_of(name: &str) -> &'static str {
    let n = name.to_ascii_lowercase();
    if n.contains("nvidia") {
        "nvidia"
    } else if n.contains("advanced micro devices") || n.contains("amd") || n.contains("radeon") {
        "amd"
    } else if n.contains("intel") {
        "intel"
    } else if n.contains("apple") {
        "apple"
    } else if n.contains("qualcomm") || n.contains("adreno") || n.contains("snapdragon") {
        "qualcomm"
    } else if n.contains("rockchip") || n.contains("rknpu") {
        "rockchip"
    } else {
        "unknown"
    }
}

fn backend_label(id: &str) -> &'static str {
    match id {
        "cuda" => "CUDA",
        "metal" => "Metal",
        "rocm" => "ROCm",
        "vulkan" => "Vulkan",
        "openvino" => "OpenVINO",
        "qnn-snapdragon" => "QNN",
        "rknpu" => "RKNPU",
        "ane" => "ANE",
        _ => "CPU",
    }
}

fn gpu_backend(vendor: &str, os: &str) -> (&'static str, bool, &'static str) {
    match vendor {
        "nvidia" => ("cuda", true, "需要 CUDA 档"),
        "apple" => ("metal", true, "系统自带 Metal"),
        "amd" if os == "linux" => ("rocm", true, "需要 ROCm 档"),
        "amd" => ("vulkan", true, "需要 Vulkan 档"),
        "intel" => ("vulkan", true, "需要 Vulkan 档"),
        "qualcomm" => ("qnn-snapdragon", true, "需要 QNN 档"),
        _ => ("vulkan", true, "需要 Vulkan 档"),
    }
}

fn push_unique(out: &mut Vec<Accelerator>, item: Accelerator) {
    if out
        .iter()
        .any(|row| row.name.eq_ignore_ascii_case(&item.name))
    {
        return;
    }
    out.push(item);
}

fn parse_nvidia(text: &str, out: &mut Vec<Accelerator>) {
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let (name, memory) = match line.rsplit_once(',') {
            Some((name, tail)) => (name.trim(), tail.trim()),
            None => (line, ""),
        };
        if name.is_empty() {
            continue;
        }
        let memory_mib = memory
            .split_whitespace()
            .next()
            .and_then(|value| value.parse::<u64>().ok());
        push_unique(
            out,
            Accelerator {
                kind: "gpu".into(),
                vendor: "nvidia".into(),
                name: name.to_string(),
                memory_mib,
                backend: "cuda".into(),
                usable: true,
                note: "需要 CUDA 档".into(),
            },
        );
    }
}

fn parse_vulkan(text: &str, os: &str, out: &mut Vec<Accelerator>) {
    for line in text.lines() {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        if !key.trim().eq_ignore_ascii_case("deviceName") {
            continue;
        }
        let name = value.trim();
        if name.is_empty() {
            continue;
        }
        let vendor = vendor_of(name);
        let (backend, usable, note) = gpu_backend(vendor, os);
        push_unique(
            out,
            Accelerator {
                kind: "gpu".into(),
                vendor: vendor.into(),
                name: name.to_string(),
                memory_mib: None,
                backend: backend.into(),
                usable,
                note: note.into(),
            },
        );
    }
}

fn parse_lspci(text: &str, os: &str, out: &mut Vec<Accelerator>) {
    for line in text.lines() {
        let lower = line.to_ascii_lowercase();
        if !(lower.contains("vga compatible controller")
            || lower.contains("3d controller")
            || lower.contains("display controller"))
        {
            continue;
        }
        let Some((_, name)) = line.split_once(": ") else {
            continue;
        };
        let name = name.trim();
        if name.is_empty() {
            continue;
        }
        let vendor = vendor_of(name);
        let (backend, usable, note) = gpu_backend(vendor, os);
        push_unique(
            out,
            Accelerator {
                kind: "gpu".into(),
                vendor: vendor.into(),
                name: name.to_string(),
                memory_mib: None,
                backend: backend.into(),
                usable,
                note: note.into(),
            },
        );
    }
}

fn parse_npu(name: &str) -> Accelerator {
    let vendor = vendor_of(name);
    let (backend, usable, note) = match vendor {
        "rockchip" => ("rknpu", false, "当前引擎没有 RKNPU 后端"),
        "qualcomm" => ("qnn-snapdragon", true, "需要 QNN 档"),
        "intel" => ("openvino", false, "当前引擎的 OpenVINO 档面向 CPU/iGPU"),
        "apple" => ("ane", false, "系统框架可用，但当前引擎不走 ANE"),
        _ => ("openvino", false, "需要上游单独提供 NPU 后端"),
    };
    Accelerator {
        kind: "npu".into(),
        vendor: vendor.into(),
        name: name.to_string(),
        memory_mib: None,
        backend: backend.into(),
        usable,
        note: note.into(),
    }
}

/// 纯函数：所有平台差异都在 `Signals` 里。
pub fn classify(signals: &Signals) -> Hardware {
    let mut accelerators: Vec<Accelerator> = Vec::new();
    if let Some(text) = &signals.nvidia_smi {
        parse_nvidia(text, &mut accelerators);
    }
    for name in &signals.windows_gpus {
        let name = name.trim();
        if name.is_empty()
            || name.eq_ignore_ascii_case("Microsoft Basic Display Adapter")
            || name.eq_ignore_ascii_case("Microsoft Remote Display Adapter")
        {
            continue;
        }
        let vendor = vendor_of(name);
        let (backend, usable, note) = gpu_backend(vendor, &signals.os);
        push_unique(
            &mut accelerators,
            Accelerator {
                kind: "gpu".into(),
                vendor: vendor.into(),
                name: name.to_string(),
                memory_mib: None,
                backend: backend.into(),
                usable,
                note: note.into(),
            },
        );
    }
    if let Some(text) = &signals.vulkaninfo {
        parse_vulkan(text, &signals.os, &mut accelerators);
    }
    if let Some(text) = &signals.lspci {
        parse_lspci(text, &signals.os, &mut accelerators);
    }
    if signals.apple_silicon {
        push_unique(
            &mut accelerators,
            Accelerator {
                kind: "gpu".into(),
                vendor: "apple".into(),
                name: "Apple Silicon GPU".into(),
                memory_mib: None,
                backend: "metal".into(),
                usable: true,
                note: "系统自带 Metal".into(),
            },
        );
        push_unique(
            &mut accelerators,
            parse_npu("Apple Neural Engine"),
        );
    }
    for name in &signals.accel_devices {
        push_unique(&mut accelerators, parse_npu(name));
    }

    let chosen = accelerators
        .iter()
        .find(|row| row.kind == "gpu" && row.usable)
        .or_else(|| accelerators.iter().find(|row| row.kind == "npu" && row.usable));
    let backend = match chosen {
        Some(row) => Backend {
            id: row.backend.clone(),
            label: backend_label(&row.backend).to_string(),
            accelerated: true,
            reason: format!("检测到 {} {}（{}）", row.vendor, row.name, row.note),
        },
        None => {
            let reason = if accelerators.is_empty() {
                "未检测到 GPU 或 NPU，走 CPU".to_string()
            } else {
                format!(
                    "检测到 {} 个加速器，但当前引擎没有可用后端，走 CPU",
                    accelerators.len()
                )
            };
            Backend {
                id: "cpu".into(),
                label: backend_label("cpu").to_string(),
                accelerated: false,
                reason,
            }
        }
    };

    Hardware {
        os: signals.os.clone(),
        arch: signals.arch.clone(),
        cpu: Cpu {
            arch: signals.arch.clone(),
            cores: signals.cores.max(1),
            brand: signals.cpu_brand.clone(),
        },
        ram_mib: signals
            .ram_bytes
            .map(|bytes| bytes / (1024 * 1024)),
        accelerators,
        backend,
    }
}

fn run(program: &str, args: &[&str]) -> Option<String> {
    let output = Command::new(program).args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

pub(crate) fn parse_meminfo(text: &str) -> Option<u64> {
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            let kib = rest.split_whitespace().next()?.parse::<u64>().ok()?;
            return Some(kib * 1024);
        }
    }
    None
}

fn cpu_brand(os: &str) -> Option<String> {
    match os {
        "linux" => {
            let text = fs::read_to_string("/proc/cpuinfo").ok()?;
            text.lines().find_map(|line| {
                let rest = line.strip_prefix("model name")?;
                let (_, value) = rest.split_once(':')?;
                let value = value.trim();
                (!value.is_empty()).then(|| value.to_string())
            })
        }
        "macos" => run("sysctl", &["-n", "machdep.cpu.brand_string"]),
        "windows" => run(
            "powershell",
            &["-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
        ),
        _ => None,
    }
}

fn ram_bytes(os: &str) -> Option<u64> {
    match os {
        "linux" => parse_meminfo(&fs::read_to_string("/proc/meminfo").ok()?),
        "macos" => run("sysctl", &["-n", "hw.memsize"])?.parse().ok(),
        "windows" => run(
            "powershell",
            &[
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
            ],
        )?
        .parse()
        .ok(),
        _ => None,
    }
}

fn windows_gpus(os: &str) -> Vec<String> {
    if os != "windows" {
        return Vec::new();
    }
    run(
        "powershell",
        &[
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_VideoController).Name",
        ],
    )
    .map(|text| text.lines().map(|line| line.trim().to_string()).collect())
    .unwrap_or_default()
}

fn accel_devices(os: &str) -> Vec<String> {
    let mut out = Vec::new();
    if os == "windows" {
        if let Some(text) = run(
            "powershell",
            &[
                "-NoProfile",
                "-Command",
                "(Get-PnpDevice -Class ComputeAccelerator -ErrorAction SilentlyContinue).FriendlyName",
            ],
        ) {
            out.extend(text.lines().map(|line| line.trim().to_string()));
        }
        return out;
    }
    if os != "linux" {
        return out;
    }
    for path in ["/dev/rknpu", "/dev/rknpu0"] {
        if std::path::Path::new(path).exists() {
            out.push("Rockchip RKNPU".to_string());
        }
    }
    for dir in ["/dev/accel", "/sys/class/accel"] {
        if let Ok(rows) = fs::read_dir(dir) {
            for row in rows.flatten() {
                let name = row.file_name().to_string_lossy().to_string();
                if name.is_empty() {
                    continue;
                }
                out.push(format!("{dir}/{name}"));
            }
        }
    }
    out
}

/// 读一次本机状态。任何一条命令失败都只让对应字段为空，不 panic。
pub fn detect() -> Hardware {
    let os = std::env::consts::OS.to_string();
    let arch = std::env::consts::ARCH.to_string();
    let signals = Signals {
        os: os.clone(),
        arch: arch.clone(),
        cores: std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1),
        cpu_brand: cpu_brand(&os),
        ram_bytes: ram_bytes(&os),
        nvidia_smi: run(
            "nvidia-smi",
            &[
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
        ),
        vulkaninfo: run("vulkaninfo", &["--summary"]),
        lspci: run("lspci", &[]),
        windows_gpus: windows_gpus(&os),
        accel_devices: accel_devices(&os),
        apple_silicon: os == "macos" && arch == "aarch64",
    };
    classify(&signals)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> Signals {
        Signals {
            os: "linux".into(),
            arch: "x86_64".into(),
            cores: 8,
            cpu_brand: Some("Test CPU".into()),
            ram_bytes: Some(16 * 1024 * 1024 * 1024),
            ..Default::default()
        }
    }

    #[test]
    fn nvidia_is_chosen_over_cpu() {
        let mut s = base();
        s.nvidia_smi = Some("NVIDIA GeForce RTX 4060, 8188\n".into());
        let hw = classify(&s);
        assert_eq!(hw.backend.id, "cuda");
        assert!(hw.backend.accelerated);
        assert_eq!(hw.accelerators.len(), 1);
        assert_eq!(hw.accelerators[0].memory_mib, Some(8188));
        assert_eq!(hw.ram_mib, Some(16384));
    }

    #[test]
    fn apple_silicon_reports_metal_and_ane() {
        let mut s = base();
        s.os = "macos".into();
        s.arch = "aarch64".into();
        s.apple_silicon = true;
        let hw = classify(&s);
        assert_eq!(hw.backend.id, "metal");
        assert!(hw.accelerators.iter().any(|a| a.kind == "gpu" && a.backend == "metal"));
        let ane = hw.accelerators.iter().find(|a| a.backend == "ane").unwrap();
        assert!(!ane.usable);
    }

    #[test]
    fn amd_gets_rocm_on_linux_and_vulkan_on_windows() {
        let mut s = base();
        s.os = "windows".into();
        s.windows_gpus = vec!["AMD Radeon RX 7800 XT".into()];
        let hw = classify(&s);
        assert_eq!(hw.backend.id, "vulkan");

        let mut s = base();
        s.nvidia_smi = None;
        s.vulkaninfo = Some("deviceName = AMD Radeon RX 7800 XT\n".into());
        let hw = classify(&s);
        assert_eq!(hw.backend.id, "rocm");
    }

    #[test]
    fn intel_igpu_gets_vulkan() {
        let mut s = base();
        s.vulkaninfo = Some("deviceName = Intel(R) Iris(R) Xe Graphics\n".into());
        let hw = classify(&s);
        assert_eq!(hw.backend.id, "vulkan");
        assert_eq!(hw.accelerators[0].vendor, "intel");
    }

    #[test]
    fn rockchip_npu_is_reported_but_not_usable() {
        let mut s = base();
        s.accel_devices = vec!["Rockchip RKNPU".into()];
        let hw = classify(&s);
        let npu = hw.accelerators.iter().find(|a| a.kind == "npu").unwrap();
        assert_eq!(npu.backend, "rknpu");
        assert!(!npu.usable);
        assert_eq!(hw.backend.id, "cpu");
        assert!(hw.backend.reason.contains("走 CPU"));
    }

    #[test]
    fn qualcomm_npu_is_usable_with_qnn() {
        let mut s = base();
        s.accel_devices = vec!["Qualcomm Hexagon NPU".into()];
        let hw = classify(&s);
        assert_eq!(hw.backend.id, "qnn-snapdragon");
        assert!(hw.backend.accelerated);
    }

    #[test]
    fn no_accelerator_falls_back_to_cpu() {
        let hw = classify(&base());
        assert_eq!(hw.backend.id, "cpu");
        assert!(!hw.backend.accelerated);
        assert!(hw.accelerators.is_empty());
    }

    #[test]
    fn nvidia_is_not_duplicated_by_vulkan_summary() {
        let mut s = base();
        s.nvidia_smi = Some("NVIDIA GeForce RTX 4090, 24564\n".into());
        s.vulkaninfo = Some("deviceName = NVIDIA GeForce RTX 4090\n".into());
        let hw = classify(&s);
        assert_eq!(hw.accelerators.len(), 1);
        assert_eq!(hw.accelerators[0].memory_mib, Some(24564));
    }

    #[test]
    fn lspci_vendor_is_parsed() {
        let mut s = base();
        s.lspci = Some(
            "01:00.0 VGA compatible controller: NVIDIA Corporation GA102 [GeForce RTX 3090] (rev a1)\n"
                .into(),
        );
        let hw = classify(&s);
        assert_eq!(hw.backend.id, "cuda");
        assert_eq!(hw.accelerators[0].vendor, "nvidia");
    }

    #[test]
    fn basic_display_adapter_is_ignored() {
        let mut s = base();
        s.os = "windows".into();
        s.windows_gpus = vec!["Microsoft Basic Display Adapter".into()];
        let hw = classify(&s);
        assert!(hw.accelerators.is_empty());
        assert_eq!(hw.backend.id, "cpu");
    }

    #[test]
    fn meminfo_is_parsed_in_bytes() {
        assert_eq!(
            parse_meminfo("MemTotal:       16315456 kB\nMemFree: 1 kB\n"),
            Some(16315456 * 1024)
        );
        assert_eq!(parse_meminfo("MemFree: 1 kB\n"), None);
    }
}
