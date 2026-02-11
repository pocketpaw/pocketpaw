"""System status tool."""

import platform
from datetime import UTC, datetime, timedelta


def get_system_status() -> str:
    """Get formatted system status."""
    try:
        import psutil
    except ImportError:
        system = platform.system()
        machine = platform.machine()
        return (
            f"🟡 **System Status (limited)**\n\n"
            f"💻 **{system} ({machine})**\n\n"
            f"Install psutil for full stats: pip install 'pocketpaw[desktop]'"
        )

    # CPU - use interval=0 to avoid blocking (uses cached value)
    cpu_percent = psutil.cpu_percent(interval=0)
    cpu_count = psutil.cpu_count()

    # Memory
    mem = psutil.virtual_memory()
    mem_used_gb = mem.used / (1024**3)
    mem_total_gb = mem.total / (1024**3)

    # Disk
    disk = psutil.disk_usage("/")
    disk_used_gb = disk.used / (1024**3)
    disk_total_gb = disk.total / (1024**3)

    # Uptime
    boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
    uptime = datetime.now(tz=UTC) - boot_time
    uptime_str = str(timedelta(seconds=int(uptime.total_seconds())))

    # Battery (if available)
    battery_str = ""
    try:
        battery = psutil.sensors_battery()
        if battery:
            battery_str = f"\n🔋 Battery: {battery.percent:.0f}%"
            if battery.power_plugged:
                battery_str += " ⚡"
    except Exception:
        pass

    # Platform info
    system = platform.system()
    machine = platform.machine()

    return f"""🟢 **System Status**

💻 **{system} ({machine})**

🧠 CPU: {cpu_percent:.1f}% ({cpu_count} cores)
💾 RAM: {mem_used_gb:.1f} / {mem_total_gb:.1f} GB ({mem.percent:.0f}%)
💿 Disk: {disk_used_gb:.0f} / {disk_total_gb:.0f} GB ({disk.percent:.0f}%){battery_str}
⏱️ Uptime: {uptime_str}
"""
