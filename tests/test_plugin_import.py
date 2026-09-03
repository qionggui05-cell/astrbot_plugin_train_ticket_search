"""模拟 AstrBot v4 的包式插件加载，验证 main.py 相对导入正常（回归：No module named 'core'）。"""

import importlib
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# 桩 astrbot 包：仅提供 main.py 导入所需的符号（装饰器均为空操作）
STUB_FILES = {
    "astrbot/__init__.py": "",
    "astrbot/api/__init__.py": "",
    "astrbot/api/star/__init__.py": (
        "import logging\n"
        "\n"
        "class Star:\n"
        "    def __init__(self, context, config=None):\n"
        "        self.context = context\n"
        "        self.logger = logging.getLogger('astrbot_test_plugin')\n"
        "\n"
        "class AstrMessageEvent:\n"
        "    pass\n"
        "\n"
        "class Context:\n"
        "    pass\n"
        "\n"
        "def register(*a, **k):\n"
        "    def deco(cls):\n"
        "        return cls\n"
        "    return deco\n"
    ),
    "astrbot/api/event/__init__.py": (
        "class AstrMessageEvent:\n"
        "    pass\n"
        "\n"
        "class MessageChain:\n"
        "    def message(self, text):\n"
        "        return self\n"
    ),
    "astrbot/api/event/filter/__init__.py": (
        "def command_group(name):\n"
        "    class _G:\n"
        "        def command(self, *a, **k):\n"
        "            def deco(f):\n"
        "                return f\n"
        "            return deco\n"
        "    def deco(f):\n"
        "        return _G()\n"
        "    return deco\n"
        "\n"
        "def regex(pattern):\n"
        "    def deco(f):\n"
        "        return f\n"
        "    return deco\n"
    ),
}


def _cleanup_modules(prefixes):
    for k in list(sys.modules):
        if k.startswith(prefixes):
            del sys.modules[k]


def test_plugin_imports_as_v4_package(tmp_path):
    data_root = tmp_path / "root"
    (data_root / "data" / "plugins").mkdir(parents=True)
    (data_root / "data").joinpath("__init__.py").write_text("", encoding="utf-8")
    (data_root / "data" / "plugins").joinpath("__init__.py").write_text(
        "", encoding="utf-8"
    )
    for rel, content in STUB_FILES.items():
        p = data_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    shutil.copytree(
        PLUGIN_ROOT,
        data_root / "data" / "plugins" / "astrbot_plugin_train_ticket_search",
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )

    sys.path.insert(0, str(data_root))
    try:
        mod = importlib.import_module(
            "data.plugins.astrbot_plugin_train_ticket_search.main"
        )
        assert hasattr(mod, "TrainTicketSearchPlugin")
        # 实例化插件，覆盖 __init__ 的完整初始化路径
        plugin = mod.TrainTicketSearchPlugin(mod.Context(), {})
        assert plugin.provider.name == "juhe"
        assert plugin.scheduler.interval_hours == 4
        assert plugin.max_locks == 20
        assert plugin.ticket_alert_threshold == 10
    finally:
        sys.path.remove(str(data_root))
        _cleanup_modules(
            (
                "data.plugins.astrbot_plugin_train_ticket_search",
                "astrbot",
                "data.plugins",
                "data",
            )
        )
