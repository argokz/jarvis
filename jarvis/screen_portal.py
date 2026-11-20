"""Снимок экрана через портал рабочего стола (org.freedesktop.portal.Screenshot).

Запускается отдельным процессом системным Python: нужен PyGObject (python3-gi), которого нет в
окружении ядра. На Wayland другого пути нет: программам экран не виден, снимок делает сам рабочий
стол и, если считает нужным, спрашивает разрешение у человека за компьютером.

Печатает путь к файлу снимка. Код выхода: 0 — готово, 2 — отказано или отменено, 3 — нет ответа,
4 — портал недоступен.
"""
import os
import sys
from urllib.parse import unquote, urlsplit

PORTAL = "org.freedesktop.portal.Desktop"


def main(timeout: int = 20) -> int:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as e:
        print(f"нужен пакет python3-gi: {e}", file=sys.stderr)
        return 4
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        token = f"jarvis{os.getpid()}"
        # Ответ придёт сигналом на объект запроса; его путь известен заранее, подписываемся до вызова.
        path = f"/org/freedesktop/portal/desktop/request/{bus.get_unique_name()[1:].replace('.', '_')}/{token}"
        loop, result = GLib.MainLoop(), {}

        def on_response(_bus, _sender, _path, _iface, _signal, params):
            result["code"], data = params.unpack()
            result["uri"] = data.get("uri", "")
            loop.quit()

        bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response", path, None,
                             Gio.DBusSignalFlags.NONE, on_response)
        options = {"handle_token": GLib.Variant("s", token), "interactive": GLib.Variant("b", False),
                   "modal": GLib.Variant("b", False)}
        bus.call_sync(PORTAL, "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Screenshot", "Screenshot",
                      GLib.Variant("(sa{sv})", ("", options)), GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE,
                      5000, None)
        GLib.timeout_add_seconds(timeout, loop.quit)
        loop.run()
        if "code" not in result:  # никто не ответил на вопрос о разрешении — убираем его с экрана
            try:
                bus.call_sync(PORTAL, path, "org.freedesktop.portal.Request", "Close", None, None,
                              Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
            return 3
    except GLib.Error as e:
        print(f"портал недоступен: {e.message}", file=sys.stderr)
        return 4
    if result["code"] != 0 or not result["uri"]:
        return 2
    print(unquote(urlsplit(result["uri"]).path))
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20))
