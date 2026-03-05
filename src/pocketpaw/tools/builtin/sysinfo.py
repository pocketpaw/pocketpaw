"""System information and resource monitoring tool."""

from typing import Any

from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.status import get_system_status


class SystemInfoTool(BaseTool):
    """Report system resource usage (CPU, RAM, disk, network, top processes)."""

    @property
    def name(self) -> str:
        return "system_info"

    @property
    def description(self) -> str:
        return (
            "Get current system resource information including CPU, RAM, disk usage, "
            "network I/O, and optionally top processes by CPU usage."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_processes": {
                    "type": "boolean",
                    "description": "Include top processes by CPU usage (default: false)",
                    "default": False,
                },
            },
            "required": [],
        }

    async def execute(self, include_processes: bool = False) -> str:
        try:
            result = get_system_status()

            # Append extra sections when psutil is available
            try:
                import psutil
            except ImportError:
                return result

            sections: list[str] = [result.rstrip()]

            # Network I/O
            try:
                net = psutil.net_io_counters()
                sent_mb = net.bytes_sent / (1024**2)
                recv_mb = net.bytes_recv / (1024**2)
                sections.append(
                    f"🌐 Network: ↑ {sent_mb:.1f} MB sent, ↓ {recv_mb:.1f} MB received"
                )
            except Exception:
                pass

            # Top processes
            if include_processes:
                try:
                    procs: list[tuple[float, str, int]] = []
                    for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
                        info = proc.info
                        cpu = info.get("cpu_percent") or 0.0
                        if cpu > 0:
                            procs.append((cpu, info.get("name", "?"), info.get("pid", 0)))
                    procs.sort(reverse=True)
                    top = procs[:5]
                    if top:
                        lines = ["📊 Top processes:"]
                        for cpu, pname, pid in top:
                            lines.append(f"  {cpu:5.1f}%  {pname} (pid {pid})")
                        sections.append("\n".join(lines))
                except Exception:
                    pass

            return "\n".join(sections)

        except Exception as e:
            return self._error(str(e))
