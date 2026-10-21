"""仓库里全部 workflow 文件名 —— action 钉表、YAML 闸门、bash 闸门读同一张表。

起因：`action_pins.WORKFLOWS` 少列了 `shell.yml`，那条 workflow 的 4 处 `uses:`
从未被审计，总数还写着 49。三份手抄清单各漏各的，不如只留一份；
新增车道时改这里，`tests/test_yaml_gate.py::TestWorkflowCoverage` 拿磁盘目录对表，
漏登记或拼错名字就红。
"""
from __future__ import annotations

import os

WORKFLOW_FILES = ("ci.yml", "engine-live.yml", "package.yml", "shell.yml", "installer.yml",
                  "upstream-follow.yml")


def paths(root: str) -> list[str]:
    return [os.path.join(root, ".github", "workflows", f) for f in WORKFLOW_FILES]
