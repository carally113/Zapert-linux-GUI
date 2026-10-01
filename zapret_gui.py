#!/usr/bin/env python3
"""Графическая оболочка для zapret-discord-youtube-linux (GTK3)."""

import functools
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import warnings

import gi

warnings.filterwarnings("ignore", category=DeprecationWarning)

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("Vte", "2.91")
    from gi.repository import Vte
except (ValueError, ImportError):
    Vte = None

import cairo
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

APP_NAME = "Zapret GUI"
APP_VERSION = "1.1.0"
ZAPRET_REPO_URL = "https://github.com/Sergeydigl3/zapret-discord-youtube-linux.git"
APP_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "zapret-gui")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "zapret-gui")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
RUN_LOG = os.path.join(CACHE_DIR, "run.log")
WRAPPER_DIR = os.path.join(CACHE_DIR, "bin")
ASKPASS = os.path.join(CACHE_DIR, "askpass.sh")
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", "zapret-gui.desktop")
MENU_FILE = os.path.join(GLib.get_user_data_dir(), "applications", "zapret-gui.desktop")
SUDOERS_FILE = "/etc/sudoers.d/zapret"
# установлено пакетом (.deb/.rpm): есть команда zapret-gui и системный ярлык
INSTALLED = APP_DIR.startswith("/usr/")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

USER_LISTS = [
    ("list-general-user.txt", "Домены, которые нужно обходить (по одному на строку)"),
    ("list-exclude-user.txt", "Домены-исключения (не трогать)"),
    ("ipset-exclude-user.txt", "IP/подсети-исключения"),
]

IPSET_NONE_IP = "203.0.113.113/32"
IPSET_MODES = [
    ("loaded", "Loaded — ipset + списки (по умолчанию)"),
    ("any", "Any — весь трафик"),
    ("none", "None — только списки доменов"),
]

DEFAULT_SETTINGS = {
    "zapret_dir": os.path.expanduser("~/zapret-discord-youtube-linux"),
    "close_to_tray": True,
}


# =============================================================================
# Настройки GUI
# =============================================================================

def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def desktop_entry(extra_args=""):
    if INSTALLED:
        exec_line = "zapret-gui"
    else:
        exec_line = f"python3 {shlex.quote(os.path.realpath(__file__))}"
    if extra_args:
        exec_line += " " + extra_args
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Управление zapret (обход блокировок Discord/YouTube)\n"
        f"Exec={exec_line}\n"
        f"Icon={'zapret-gui' if INSTALLED else 'network-workgroup'}\n"
        "Terminal=false\n"
        "Categories=Network;\n"
        "Keywords=zapret;youtube;discord;dpi;\n"
    )


# =============================================================================
# Работа с privilege escalation: sudo -A + графический askpass
# =============================================================================

def prepare_sudo_wrapper():
    """Создаёт обёртку sudo, которая берёт пароль через графический askpass.

    Скрипты zapret вызывают просто `sudo ...`. Подкладываем свой `sudo` в
    начало PATH: при ZGUI_SUDO_MODE=n он не спрашивает пароль вовсе
    (для фоновых проверок статуса), иначе использует sudo -A.
    """
    os.makedirs(WRAPPER_DIR, exist_ok=True)
    real_sudo = None
    for d in os.environ.get("PATH", "").split(os.pathsep) + ["/usr/bin", "/bin"]:
        cand = os.path.join(d, "sudo")
        if d != WRAPPER_DIR and os.access(cand, os.X_OK):
            real_sudo = cand
            break
    if real_sudo is None:
        return None

    wrapper = os.path.join(WRAPPER_DIR, "sudo")
    with open(wrapper, "w") as f:
        f.write(
            "#!/bin/sh\n"
            'if [ "${ZGUI_SUDO_MODE:-}" = "n" ]; then\n'
            f'    exec {shlex.quote(real_sudo)} -n "$@"\n'
            "fi\n"
            f'exec {shlex.quote(real_sudo)} -A "$@"\n'
        )
    os.chmod(wrapper, 0o755)

    with open(ASKPASS, "w") as f:
        f.write(
            "#!/bin/sh\n"
            "if command -v zenity >/dev/null 2>&1; then\n"
            "    exec zenity --password --title='Zapret GUI: требуются права root'\n"
            "elif command -v ssh-askpass >/dev/null 2>&1; then\n"
            "    exec ssh-askpass \"$1\"\n"
            "elif command -v kdialog >/dev/null 2>&1; then\n"
            "    exec kdialog --password 'Zapret GUI: пароль sudo'\n"
            "fi\n"
            "exit 1\n"
        )
    os.chmod(ASKPASS, 0o755)
    return wrapper


def priv_env(noninteractive=False):
    env = dict(os.environ)
    env["PATH"] = WRAPPER_DIR + os.pathsep + env.get("PATH", "") + ":/usr/local/sbin:/usr/sbin:/sbin"
    env["SUDO_ASKPASS"] = ASKPASS
    env["LANG"] = env.get("LANG") or "C.UTF-8"
    if noninteractive:
        env["ZGUI_SUDO_MODE"] = "n"
    return env


# =============================================================================
# Модель: обёртка над каталогом zapret-discord-youtube-linux
# =============================================================================

class Zapret:
    CONF_KEYS = ["interface", "gamefiltertcp", "gamefilterudp", "strategy", "firewall_backend"]

    def __init__(self, path):
        self.dir = os.path.realpath(os.path.expanduser(path))

    # --- пути -------------------------------------------------------------
    @property
    def service_sh(self):
        return os.path.join(self.dir, "service.sh")

    @property
    def conf_file(self):
        return os.path.join(self.dir, "conf.env")

    @property
    def repo_dir(self):
        return os.path.join(self.dir, "zapret-latest")

    @property
    def user_lists_dir(self):
        return os.path.join(self.dir, "user-lists")

    def is_valid(self):
        return os.path.isfile(self.service_sh)

    def deps_ok(self):
        return os.path.isfile(os.path.join(self.dir, "nfqws")) and os.path.isdir(self.repo_dir)

    # --- данные для формы --------------------------------------------------
    def strategies(self):
        names = set()
        custom = os.path.join(self.dir, "custom-strategies")
        if os.path.isdir(custom):
            names.update(n for n in os.listdir(custom)
                         if n.endswith(".bat") and os.path.isfile(os.path.join(custom, n)))
        if os.path.isdir(self.repo_dir):
            names.update(n for n in os.listdir(self.repo_dir)
                         if n.endswith(".bat") and (n.startswith("general") or n.startswith("discord")))
        return sorted(names, key=lambda s: s.lower())

    @staticmethod
    def interfaces():
        try:
            ifaces = sorted(os.listdir("/sys/class/net"))
        except OSError:
            ifaces = []
        return ["any"] + ifaces

    def firewall_backends(self):
        d = os.path.join(self.dir, "src", "firewall-backends")
        result = []
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                if n.endswith(".sh"):
                    base = n[:-3]
                    m = re.match(r"^\d\d-(.*)$", base)
                    result.append(m.group(1) if m else base)
        return ["auto"] + result

    # --- conf.env ------------------------------------------------------------
    def read_conf(self):
        conf = {}
        try:
            with open(self.conf_file) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        conf[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
        return conf

    def write_conf(self, conf):
        with open(self.conf_file, "w") as f:
            for k in self.CONF_KEYS:
                f.write(f"{k}={conf.get(k, '')}\n")

    # --- процессы ------------------------------------------------------------
    def _iter_procs(self):
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
                with open(f"/proc/{pid}/comm") as f:
                    comm = f.read().strip()
            except OSError:
                continue
            yield int(pid), comm, args

    def nfqws_running(self):
        return any(comm == "nfqws" for _, comm, _ in self._iter_procs())

    def manual_run_pids(self):
        """PID процессов `service.sh run`, запущенных текущим пользователем."""
        pids = []
        uid = os.getuid()
        for pid, _, args in self._iter_procs():
            if self.service_sh in args and "run" in args:
                try:
                    if os.stat(f"/proc/{pid}").st_uid == uid:
                        pids.append(pid)
                except OSError:
                    pass
        return pids

    def service_status(self):
        """'absent' | 'active' | 'inactive' | 'unknown'. Не спрашивает пароль."""
        if not self.is_valid():
            return "unknown"
        try:
            out = subprocess.run(
                ["bash", self.service_sh, "service", "status"],
                cwd=self.dir, env=priv_env(noninteractive=True),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        if "не установлен" in out:
            return "absent"
        if "не активен" in out:
            return "inactive"
        if "активен" in out:
            return "active"
        return "unknown"

    # --- ipset ---------------------------------------------------------------
    @property
    def ipset_file(self):
        return os.path.join(self.repo_dir, "lists", "ipset-all.txt")

    def ipset_mode(self):
        if not os.path.isdir(os.path.join(self.repo_dir, "lists")):
            return None
        try:
            with open(self.ipset_file) as f:
                content = f.read()
        except OSError:
            return "any"
        if IPSET_NONE_IP in content:
            return "none"
        if not content.strip():
            return "any"
        return "loaded"

    def set_ipset_mode(self, mode):
        """Та же логика, что в src/lib/ipswitch.sh, но файлы пишутся на месте,
        чтобы не рвать жёсткие ссылки."""
        ipset, backup = self.ipset_file, self.ipset_file + ".backup"
        current = self.ipset_mode()
        if mode == current:
            return
        if current == "loaded":
            shutil.copyfile(ipset, backup)
        if mode == "none":
            with open(ipset, "w") as f:
                f.write(IPSET_NONE_IP + "\n")
        elif mode == "any":
            open(ipset, "w").close()
        elif mode == "loaded":
            if not os.path.isfile(backup):
                raise RuntimeError("Не найден бэкап ipset-all.txt.backup — переустановите стратегии "
                                   "(Инструменты → Скачать зависимости).")
            shutil.copyfile(backup, ipset)


# =============================================================================
# Иконки (рисуются на лету)
# =============================================================================

STATE_COLORS = {
    "on": (0.18, 0.72, 0.36),
    "off": (0.55, 0.55, 0.58),
    "busy": (0.95, 0.62, 0.12),
    "error": (0.85, 0.25, 0.25),
}


def surface_to_pixbuf(surface):
    # без зависимости от python3-gi-cairo: через PNG в памяти
    buf = io.BytesIO()
    surface.write_to_png(buf)
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(buf.getvalue())
    loader.close()
    return loader.get_pixbuf()


@functools.lru_cache(maxsize=None)
def make_icon(state, size=64):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    r, g, b = STATE_COLORS[state]
    c = size / 2
    # щит
    cr.move_to(c, size * 0.06)
    cr.line_to(size * 0.88, size * 0.2)
    cr.curve_to(size * 0.88, size * 0.6, size * 0.7, size * 0.82, c, size * 0.95)
    cr.curve_to(size * 0.3, size * 0.82, size * 0.12, size * 0.6, size * 0.12, size * 0.2)
    cr.close_path()
    cr.set_source_rgb(r, g, b)
    cr.fill_preserve()
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.set_line_width(size * 0.04)
    cr.stroke()
    # молния
    cr.set_source_rgb(1, 1, 1)
    pts = [(0.56, 0.2), (0.34, 0.55), (0.49, 0.55), (0.42, 0.82), (0.66, 0.45), (0.51, 0.45), (0.58, 0.2)]
    cr.move_to(pts[0][0] * size, pts[0][1] * size)
    for x, y in pts[1:]:
        cr.line_to(x * size, y * size)
    cr.close_path()
    cr.fill()
    return surface_to_pixbuf(surface)


@functools.lru_cache(maxsize=None)
def status_dot(state, size=14):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.arc(size / 2, size / 2, size / 2 - 1, 0, 2 * math.pi)
    cr.set_source_rgb(*STATE_COLORS[state])
    cr.fill()
    return surface_to_pixbuf(surface)


# =============================================================================
# Главное окно
# =============================================================================

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.settings = app.settings
        self.z = Zapret(self.settings["zapret_dir"])
        self.busy = False
        self.service_state = "unknown"
        self.running = False
        self.run_log_pos = 0
        self.terminal_busy = False
        self.terminal_on_exit = None
        self.loading_form = False

        self.set_default_size(820, 640)
        self.set_icon(make_icon("off", 128))
        self.connect("delete-event", self.on_delete)

        header = Gtk.HeaderBar(show_close_button=True, title=APP_NAME, subtitle=f"v{APP_VERSION}")
        self.header_dot = Gtk.Image.new_from_pixbuf(status_dot("off"))
        header.pack_start(self.header_dot)
        self.set_titlebar(header)

        self.notebook = Gtk.Notebook()
        self.add(self.notebook)
        self.notebook.append_page(self.build_main_page(), Gtk.Label(label="Управление"))
        self.notebook.append_page(self.build_service_page(), Gtk.Label(label="Автозапуск"))
        self.notebook.append_page(self.build_lists_page(), Gtk.Label(label="Списки"))
        self.notebook.append_page(self.build_tools_page(), Gtk.Label(label="Инструменты"))
        self.notebook.append_page(self.build_settings_page(), Gtk.Label(label="Настройки"))

        self.reload_all()
        GLib.timeout_add_seconds(2, self.poll_fast)
        GLib.timeout_add_seconds(10, self.poll_service)

    # ------------------------------------------------------------------ utils
    @staticmethod
    def frame(title, child):
        fr = Gtk.Frame(label=title)
        fr.get_label_widget().set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        wrap = Gtk.Box(border_width=10)
        wrap.pack_start(child, True, True, 0)
        fr.add(wrap)
        return fr

    def message(self, text, kind=Gtk.MessageType.INFO, secondary=None):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                                buttons=Gtk.ButtonsType.OK, text=text)
        if secondary:
            dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def confirm(self, text, secondary=None):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO, text=text)
        if secondary:
            dlg.format_secondary_text(secondary)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.YES

    def log(self, text):
        text = ANSI_RE.sub("", text)
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), text if text.endswith("\n") else text + "\n")
        # ограничим размер журнала
        if buf.get_line_count() > 3000:
            buf.delete(buf.get_start_iter(), buf.get_iter_at_line(buf.get_line_count() - 2500))
        self.log_view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
        GLib.idle_add(self.scroll_log_end)

    def scroll_log_end(self):
        adj = self.log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def run_cmd(self, args, title, on_done=None):
        """Запускает service.sh <args> в фоне, вывод — в журнал."""
        if self.busy:
            self.message("Подождите, предыдущее действие ещё выполняется.")
            return
        self.set_busy(True)
        self.log(f"\n▶ {title}: service.sh {' '.join(args)}")

        def worker():
            code = -1
            try:
                proc = subprocess.Popen(
                    ["bash", self.z.service_sh] + args, cwd=self.z.dir, env=priv_env(),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace",
                )
                for line in proc.stdout:
                    GLib.idle_add(self.log, line.rstrip("\n"))
                code = proc.wait()
            except OSError as e:
                GLib.idle_add(self.log, f"Ошибка запуска: {e}")
            GLib.idle_add(finish, code)

        def finish(code):
            self.log(f"■ {title}: {'готово' if code == 0 else f'код завершения {code}'}")
            self.set_busy(False)
            self.refresh_status(full=True)
            if on_done:
                on_done(code)
            return False

        threading.Thread(target=worker, daemon=True).start()

    def set_busy(self, busy):
        self.busy = busy
        self.update_status_widgets()

    # =================================================================== pages
    def build_main_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)

        # --- предупреждения
        self.warn_bar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING, show_close_button=False)
        self.warn_label = Gtk.Label(xalign=0, wrap=True)
        self.warn_bar.get_content_area().add(self.warn_label)
        self.warn_button = self.warn_bar.add_button("Исправить", 1)
        self.warn_bar.connect("response", self.on_warn_response)
        self.warn_bar.set_no_show_all(True)
        box.pack_start(self.warn_bar, False, False, 0)

        # --- статус + большая кнопка
        status_box = Gtk.Box(spacing=16)
        self.big_icon = Gtk.Image.new_from_pixbuf(make_icon("off", 72))
        status_box.pack_start(self.big_icon, False, False, 0)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.status_title = Gtk.Label(xalign=0)
        self.status_sub = Gtk.Label(xalign=0, wrap=True)
        self.status_sub.get_style_context().add_class("dim-label")
        vb.pack_start(self.status_title, False, False, 0)
        vb.pack_start(self.status_sub, False, False, 0)
        status_box.pack_start(vb, True, True, 0)

        self.toggle_btn = Gtk.Button(label="Запустить")
        self.toggle_btn.set_size_request(160, 48)
        self.toggle_btn.set_valign(Gtk.Align.CENTER)
        self.toggle_btn.connect("clicked", self.on_toggle)
        status_box.pack_end(self.toggle_btn, False, False, 0)
        box.pack_start(status_box, False, False, 0)

        # --- конфигурация
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.strategy_combo = Gtk.ComboBoxText(hexpand=True)
        self.iface_combo = Gtk.ComboBoxText()
        self.backend_combo = Gtk.ComboBoxText()
        self.gt_check = Gtk.CheckButton(label="GameFilter TCP (порты 1024-65535)")
        self.gu_check = Gtk.CheckButton(label="GameFilter UDP (порты 1024-65535)")
        for w in (self.strategy_combo, self.iface_combo, self.backend_combo):
            w.connect("changed", self.on_form_changed)
        for w in (self.gt_check, self.gu_check):
            w.connect("toggled", self.on_form_changed)

        strat_row = Gtk.Box(spacing=6)
        strat_row.pack_start(self.strategy_combo, True, True, 0)
        prev_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        prev_btn.set_tooltip_text("Предыдущая стратегия")
        prev_btn.connect("clicked", lambda *_: self.step_strategy(-1))
        next_btn = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
        next_btn.set_tooltip_text("Следующая стратегия")
        next_btn.connect("clicked", lambda *_: self.step_strategy(1))
        strat_row.pack_start(prev_btn, False, False, 0)
        strat_row.pack_start(next_btn, False, False, 0)

        rows = [("Стратегия:", strat_row), ("Интерфейс:", self.iface_combo),
                ("Бэкенд файрвола:", self.backend_combo)]
        for i, (lbl, w) in enumerate(rows):
            grid.attach(Gtk.Label(label=lbl, xalign=0), 0, i, 1, 1)
            grid.attach(w, 1, i, 1, 1)
        grid.attach(self.gt_check, 1, 3, 1, 1)
        grid.attach(self.gu_check, 1, 4, 1, 1)

        btns = Gtk.Box(spacing=6)
        self.save_btn = Gtk.Button(label="Сохранить")
        self.save_btn.set_tooltip_text("Записать conf.env, не перезапуская")
        self.save_btn.connect("clicked", lambda *_: self.save_form())
        self.apply_btn = Gtk.Button(label="Применить и перезапустить")
        self.apply_btn.get_style_context().add_class("suggested-action")
        self.apply_btn.connect("clicked", self.on_apply)
        btns.pack_end(self.apply_btn, False, False, 0)
        btns.pack_end(self.save_btn, False, False, 0)
        self.dirty_label = Gtk.Label(xalign=0)
        btns.pack_start(self.dirty_label, False, False, 0)
        grid.attach(btns, 0, 5, 2, 1)

        box.pack_start(self.frame("Конфигурация (conf.env)", grid), False, False, 0)

        # --- журнал
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.log_scroll.add(self.log_view)
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.pack_start(self.log_scroll, True, True, 0)
        clear = Gtk.Button(label="Очистить журнал", halign=Gtk.Align.END)
        clear.connect("clicked", lambda *_: self.log_view.get_buffer().set_text(""))
        log_box.pack_start(clear, False, False, 0)
        box.pack_start(self.frame("Журнал", log_box), True, True, 0)
        return box

    def build_service_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)
        info = Gtk.Label(xalign=0, wrap=True, label=(
            "Системный сервис запускает zapret автоматически при загрузке компьютера, "
            "используя настройки из conf.env. Управление сервисом требует пароль sudo "
            "(он будет запрошен в отдельном окне)."))
        box.pack_start(info, False, False, 0)

        st = Gtk.Box(spacing=8)
        self.svc_dot = Gtk.Image.new_from_pixbuf(status_dot("off"))
        self.svc_label = Gtk.Label(xalign=0)
        st.pack_start(self.svc_dot, False, False, 0)
        st.pack_start(self.svc_label, False, False, 0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Обновить статус")
        refresh.connect("clicked", lambda *_: self.refresh_status(full=True))
        st.pack_end(refresh, False, False, 0)
        box.pack_start(self.frame("Статус сервиса", st), False, False, 0)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        self.svc_buttons = {}
        actions = [
            ("install", "Установить и запустить", "Установить сервис автозапуска"),
            ("remove", "Удалить сервис", "Удалить сервис автозапуска"),
            ("start", "Запустить", "Запуск сервиса"),
            ("stop", "Остановить", "Остановка сервиса"),
            ("restart", "Перезапустить", "Перезапуск сервиса"),
        ]
        for i, (act, label, title) in enumerate(actions):
            b = Gtk.Button(label=label)
            b.connect("clicked", self.on_service_action, act, title)
            grid.attach(b, i % 3, i // 3, 1, 1)
            self.svc_buttons[act] = b
        self.svc_buttons["install"].get_style_context().add_class("suggested-action")
        self.svc_buttons["remove"].get_style_context().add_class("destructive-action")
        box.pack_start(self.frame("Действия", grid), False, False, 0)
        return box

    def build_lists_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)

        ipset_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.ipset_radios = {}
        group = None
        for mode, label in IPSET_MODES:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
            group = group or rb
            rb.connect("toggled", self.on_ipset_toggled, mode)
            ipset_box.pack_start(rb, False, False, 0)
            self.ipset_radios[mode] = rb
        self.ipset_note = Gtk.Label(xalign=0, wrap=True)
        self.ipset_note.get_style_context().add_class("dim-label")
        ipset_box.pack_start(self.ipset_note, False, False, 0)
        box.pack_start(self.frame("Режим ipset", ipset_box), False, False, 0)

        ed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top = Gtk.Box(spacing=6)
        self.list_combo = Gtk.ComboBoxText()
        for name, _ in USER_LISTS:
            self.list_combo.append(name, name)
        self.list_combo.connect("changed", self.on_list_selected)
        top.pack_start(self.list_combo, False, False, 0)
        self.list_desc = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.list_desc.get_style_context().add_class("dim-label")
        top.pack_start(self.list_desc, True, True, 0)
        ed.pack_start(top, False, False, 0)

        self.list_view = Gtk.TextView(monospace=True)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.add(self.list_view)
        ed.pack_start(sc, True, True, 0)

        bb = Gtk.Box(spacing=6)
        hint = Gtk.Label(xalign=0, wrap=True,
                         label="Изменения применяются после перезапуска zapret.")
        hint.get_style_context().add_class("dim-label")
        bb.pack_start(hint, True, True, 0)
        save = Gtk.Button(label="Сохранить")
        save.connect("clicked", lambda *_: self.save_list(restart=False))
        save_r = Gtk.Button(label="Сохранить и перезапустить")
        save_r.get_style_context().add_class("suggested-action")
        save_r.connect("clicked", lambda *_: self.save_list(restart=True))
        bb.pack_end(save_r, False, False, 0)
        bb.pack_end(save, False, False, 0)
        ed.pack_start(bb, False, False, 0)
        box.pack_start(self.frame("Пользовательские списки", ed), True, True, 0)
        return box

    def build_tools_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3,
                           column_spacing=6, row_spacing=6, homogeneous=True)
        tools = [
            ("Обновить zapret", "Обновить скрипты (git pull), затем nfqws и стратегии рекомендуемых версий",
             self.on_update_zapret),
            ("Автоподбор для YouTube", "Перебор стратегий с проверкой YouTube (auto_tune_youtube.sh)",
             lambda: self.run_in_terminal(["./auto_tune_youtube.sh"], stop_first=True)),
            ("Автоподбор по доменам", "Проверка любых доменов (auto_tune.sh)",
             lambda: self.run_in_terminal(["./auto_tune.sh"], stop_first=True)),
            ("Скачать зависимости", "nfqws и стратегии рекомендуемых версий (download-deps --default)",
             lambda: self.run_in_terminal(["./service.sh", "download-deps", "--default"])),
            ("Выбрать версии…", "Интерактивный выбор версий nfqws и стратегий",
             lambda: self.run_in_terminal(["./service.sh", "download-deps"])),
            ("Настроить вход без пароля", "Создать /etc/sudoers.d/zapret (setup-permissions)",
             lambda: self.run_in_terminal(["./service.sh", "setup-permissions"])),
            ("Убрать вход без пароля", "Удалить /etc/sudoers.d/zapret",
             lambda: self.run_in_terminal(["./service.sh", "setup-permissions", "remove"])),
            ("Аварийная остановка", "Убить nfqws и очистить правила файрвола (service.sh kill)",
             lambda: self.run_in_terminal(["./service.sh", "kill"])),
            ("Интерактивное меню", "Оригинальное текстовое меню service.sh",
             lambda: self.run_in_terminal(["./service.sh"])),
        ]
        for label, tip, cb in tools:
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, cb=cb: cb())
            flow.add(b)
        box.pack_start(flow, False, False, 0)

        term_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if Vte is not None:
            self.terminal = Vte.Terminal()
            self.terminal.set_scrollback_lines(5000)
            self.terminal.set_size(80, 20)
            self.terminal.connect("child-exited", self.on_terminal_exit)
            sc = Gtk.ScrolledWindow(vexpand=True)
            sc.add(self.terminal)
            term_box.pack_start(sc, True, True, 0)
            tb = Gtk.Box(spacing=6)
            self.term_label = Gtk.Label(xalign=0, label="Терминал свободен")
            self.term_label.get_style_context().add_class("dim-label")
            tb.pack_start(self.term_label, True, True, 0)
            kill = Gtk.Button(label="Прервать")
            kill.connect("clicked", self.on_terminal_kill)
            tb.pack_end(kill, False, False, 0)
            term_box.pack_start(tb, False, False, 0)
        else:
            self.terminal = None
            term_box.pack_start(Gtk.Label(
                wrap=True, label="Встроенный терминал недоступен (нет пакета gir1.2-vte-2.91). "
                                 "Скрипты будут открыты во внешнем терминале."), False, False, 0)
        box.pack_start(self.frame("Терминал", term_box), True, True, 0)
        return box

    def build_settings_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.attach(Gtk.Label(label="Папка zapret-discord-youtube-linux:", xalign=0), 0, 0, 1, 1)
        self.dir_chooser = Gtk.FileChooserButton(title="Выберите папку",
                                                 action=Gtk.FileChooserAction.SELECT_FOLDER, hexpand=True)
        if os.path.isdir(self.z.dir):
            self.dir_chooser.set_filename(self.z.dir)
        self.dir_chooser.connect("file-set", self.on_dir_changed)
        grid.attach(self.dir_chooser, 1, 0, 1, 1)
        clone = Gtk.Button(label="Скачать с GitHub…")
        clone.set_tooltip_text("git clone в ~/zapret-discord-youtube-linux и загрузка зависимостей")
        clone.connect("clicked", self.on_clone)
        grid.attach(clone, 2, 0, 1, 1)
        box.pack_start(self.frame("Расположение", grid), False, False, 0)

        beh = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.tray_check = Gtk.CheckButton(label="Сворачивать в трей при закрытии окна")
        self.tray_check.set_active(self.settings.get("close_to_tray", True))
        self.tray_check.connect("toggled", self.on_tray_toggled)
        beh.pack_start(self.tray_check, False, False, 0)
        self.autostart_check = Gtk.CheckButton(label="Запускать GUI (свёрнутым в трей) при входе в систему")
        self.autostart_check.set_active(os.path.isfile(AUTOSTART_FILE))
        self.autostart_check.connect("toggled", self.on_autostart_toggled)
        beh.pack_start(self.autostart_check, False, False, 0)
        self.menu_check = Gtk.CheckButton(label="Ярлык в меню приложений")
        self.menu_check.set_active(os.path.isfile(MENU_FILE))
        self.menu_check.connect("toggled", self.on_menu_toggled)
        if not INSTALLED:
            beh.pack_start(self.menu_check, False, False, 0)
        box.pack_start(self.frame("Поведение", beh), False, False, 0)

        priv = Gtk.Label(xalign=0, wrap=True, label=(
            "Права root нужны для nft и nfqws. GUI вызывает sudo; если пароль требуется, "
            "он запрашивается в графическом окне. Чтобы запуск/остановка шли без пароля, "
            "используйте «Инструменты → Настроить вход без пароля». "
            "Управление системным сервисом всегда запрашивает пароль."))
        box.pack_start(self.frame("Права доступа", priv), False, False, 0)
        return box

    # ============================================================ data loading
    def reload_all(self):
        self.loading_form = True
        conf = self.z.read_conf()

        def fill(combo, items, current, fallback=None):
            combo.remove_all()
            for it in items:
                combo.append(it, it)
            if current and current not in items:
                combo.append(current, f"{current} (не найдено)")
            if not combo.set_active_id(current or "") and fallback is not None:
                combo.set_active_id(fallback) or combo.set_active(0)

        fill(self.strategy_combo, self.z.strategies(), conf.get("strategy"), "general.bat")
        fill(self.iface_combo, self.z.interfaces(), conf.get("interface"), "any")
        fill(self.backend_combo, self.z.firewall_backends(), conf.get("firewall_backend", "auto"), "auto")
        self.gt_check.set_active(conf.get("gamefiltertcp") == "true")
        self.gu_check.set_active(conf.get("gamefilterudp") == "true")
        self.loading_form = False
        self.form_dirty = not os.path.isfile(self.z.conf_file)
        self.update_dirty()

        if self.list_combo.get_active_id() is None:
            self.list_combo.set_active(0)
        else:
            self.on_list_selected(self.list_combo)
        self.load_ipset()
        self.refresh_status(full=True)

    def form_values(self):
        return {
            "interface": self.iface_combo.get_active_id() or "any",
            "gamefiltertcp": "true" if self.gt_check.get_active() else "false",
            "gamefilterudp": "true" if self.gu_check.get_active() else "false",
            "strategy": self.strategy_combo.get_active_id() or "",
            "firewall_backend": self.backend_combo.get_active_id() or "auto",
        }

    def on_form_changed(self, *_):
        if not self.loading_form:
            self.form_dirty = True
            self.update_dirty()

    def update_dirty(self):
        self.dirty_label.set_markup("<i>Есть несохранённые изменения</i>" if self.form_dirty else "")

    def step_strategy(self, delta):
        model = self.strategy_combo.get_model()
        n = len(model)
        if n:
            i = self.strategy_combo.get_active()
            self.strategy_combo.set_active((i + delta) % n if i >= 0 else 0)

    def save_form(self):
        vals = self.form_values()
        if not vals["strategy"]:
            self.message("Не выбрана стратегия.", Gtk.MessageType.ERROR,
                         "Скачайте зависимости: Инструменты → Скачать зависимости.")
            return False
        try:
            self.z.write_conf(vals)
        except OSError as e:
            self.message("Не удалось записать conf.env", Gtk.MessageType.ERROR, str(e))
            return False
        self.form_dirty = False
        self.update_dirty()
        self.log(f"Конфигурация сохранена: {vals['strategy']}, {vals['interface']}, "
                 f"TCP={vals['gamefiltertcp']}, UDP={vals['gamefilterudp']}, fw={vals['firewall_backend']}")
        return True

    # ================================================================= status
    def poll_fast(self):
        self.refresh_status(full=False)
        self.tail_run_log()
        return True

    def poll_service(self):
        if self.z.is_valid() and not self.busy:
            self.refresh_status(full=True)
        return True

    def refresh_status(self, full=False):
        self.running = self.z.nfqws_running() if self.z.is_valid() else False
        if full:
            def worker():
                st = self.z.service_status()
                GLib.idle_add(self.set_service_state, st)
            threading.Thread(target=worker, daemon=True).start()
        self.update_status_widgets()

    def set_service_state(self, st):
        self.service_state = st
        self.update_status_widgets()
        return False

    def mode(self):
        """Как сейчас запущен zapret: 'service' | 'manual' | 'other' | None."""
        if self.service_state == "active":
            return "service"
        if self.z.manual_run_pids():
            return "manual"
        if self.running:
            return "other"
        return None

    def current_state_key(self):
        if self.busy:
            return "busy"
        if not self.z.is_valid() or not self.z.deps_ok():
            return "error"
        return "on" if self.running else "off"

    def update_status_widgets(self):
        key = self.current_state_key()
        mode = self.mode()
        conf = self.z.read_conf()

        if not self.z.is_valid():
            title, sub = "Не найден zapret-discord-youtube-linux", f"Нет файла {self.z.service_sh}"
        elif not self.z.deps_ok():
            title, sub = "Зависимости не скачаны", "Нужны nfqws и стратегии"
        elif self.busy:
            title, sub = "Выполняется…", "Подождите завершения действия"
        elif self.running:
            how = {"service": "через системный сервис", "manual": "вручную из GUI",
                   "other": "запущен вне GUI"}.get(mode, "")
            title = "Zapret работает"
            sub = f"Стратегия: {conf.get('strategy', '—')} · {how}"
        else:
            title, sub = "Zapret остановлен", f"Стратегия: {conf.get('strategy', '—')}"
        self.status_title.set_markup(f"<span size='x-large' weight='bold'>{GLib.markup_escape_text(title)}</span>")
        self.status_sub.set_text(sub)
        self.big_icon.set_from_pixbuf(make_icon(key, 72))
        self.header_dot.set_from_pixbuf(status_dot(key))
        self.set_icon(make_icon(key, 128))

        self.toggle_btn.set_label("Остановить" if self.running else "Запустить")
        ctx = self.toggle_btn.get_style_context()
        ctx.remove_class("suggested-action")
        ctx.remove_class("destructive-action")
        ctx.add_class("destructive-action" if self.running else "suggested-action")
        ok = self.z.is_valid() and self.z.deps_ok() and not self.busy
        self.toggle_btn.set_sensitive(ok)
        self.apply_btn.set_sensitive(ok)
        self.save_btn.set_sensitive(self.z.is_valid() and self.z.deps_ok())

        # предупреждения
        warn = None
        if not self.z.is_valid():
            warn = ("Папка zapret-discord-youtube-linux не найдена. Укажите её в настройках "
                    "или скачайте с GitHub.", "settings")
        elif not self.z.deps_ok():
            warn = ("Не скачаны nfqws и стратегии.", "deps")
        elif not os.path.exists(SUDOERS_FILE) and os.geteuid() != 0:
            warn = ("Вход без пароля не настроен — при каждом запуске/остановке будет запрашиваться "
                    "пароль sudo.", "perms")
        if warn:
            self.warn_label.set_text(warn[0])
            self.warn_action = warn[1]
            self.warn_bar.show()
            self.warn_bar.get_content_area().show_all()
            self.warn_button.show()
        else:
            self.warn_bar.hide()

        # сервис
        svc_text = {
            "absent": ("Сервис не установлен", "off"),
            "active": ("Сервис установлен и активен", "on"),
            "inactive": ("Сервис установлен, но не активен", "busy"),
            "unknown": ("Статус неизвестен", "error"),
        }[self.service_state]
        self.svc_label.set_text(svc_text[0])
        self.svc_dot.set_from_pixbuf(status_dot(svc_text[1]))
        installed = self.service_state in ("active", "inactive")
        can = self.z.is_valid() and self.z.deps_ok() and not self.busy
        self.svc_buttons["install"].set_sensitive(can and not installed)
        self.svc_buttons["remove"].set_sensitive(can and installed)
        self.svc_buttons["start"].set_sensitive(can and self.service_state == "inactive")
        self.svc_buttons["stop"].set_sensitive(can and self.service_state == "active")
        self.svc_buttons["restart"].set_sensitive(can and installed)

        self.app.update_tray(key, title, self.running)

    def first_run_check(self):
        """Мастер первого запуска: скачать zapret, затем настроить вход без пароля."""
        if not self.z.is_valid():
            target = os.path.expanduser("~/zapret-discord-youtube-linux")
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE, text="Добро пожаловать в Zapret GUI!")
            dlg.format_secondary_text(
                "Для работы нужен zapret-discord-youtube-linux. Скачать его сейчас в "
                f"{target}?\n\nБудут загружены скрипты, nfqws и стратегии (нужен интернет и git). "
                "Если он уже скачан в другое место — укажите папку в настройках.")
            dlg.add_buttons("Указать папку…", 1, "Скачать", Gtk.ResponseType.YES)
            dlg.set_default_response(Gtk.ResponseType.YES)
            resp = dlg.run()
            dlg.destroy()
            if resp == Gtk.ResponseType.YES:
                self.on_clone()
            elif resp == 1:
                self.notebook.set_current_page(4)
            return False
        if not self.z.deps_ok():
            if self.confirm("Не скачаны nfqws и стратегии.", "Скачать рекомендуемые версии сейчас?"):
                self.run_in_terminal(["./service.sh", "download-deps", "--default"])
            return False
        if (not os.path.exists(SUDOERS_FILE) and os.geteuid() != 0
                and not self.settings.get("perms_asked")):
            self.settings["perms_asked"] = True
            save_settings(self.settings)
            if self.confirm("Настроить запуск без пароля?",
                            "Сейчас для каждого запуска и остановки zapret нужен пароль sudo. "
                            "Можно один раз разрешить без пароля только nft и nfqws "
                            "(файл /etc/sudoers.d/zapret). Пароль будет запрошен в терминале."):
                self.run_in_terminal(["./service.sh", "setup-permissions"])
        return False

    def on_warn_response(self, _bar, _resp):
        action = getattr(self, "warn_action", None)
        if action == "settings":
            self.first_run_check()
        elif action == "deps":
            self.notebook.set_current_page(3)
            self.run_in_terminal(["./service.sh", "download-deps", "--default"])
        elif action == "perms":
            self.notebook.set_current_page(3)
            self.run_in_terminal(["./service.sh", "setup-permissions"])

    # ============================================================ run / stop
    def on_toggle(self, *_):
        if self.running:
            self.stop()
        else:
            self.start()

    def on_apply(self, *_):
        if not self.save_form():
            return
        if self.running:
            self.restart()
        else:
            self.start()

    def start(self):
        if self.form_dirty or not os.path.isfile(self.z.conf_file):
            if not self.save_form():
                return
        if self.service_state in ("active", "inactive"):
            self.run_cmd(["service", "start"], "Запуск сервиса")
            return
        self.start_manual()

    def start_manual(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        logf = open(RUN_LOG, "w")
        self.run_log_pos = 0
        self.log("\n▶ Запуск zapret (service.sh run --config conf.env)")
        try:
            subprocess.Popen(
                ["bash", self.z.service_sh, "run", "--config", self.z.conf_file],
                cwd=self.z.dir, env=priv_env(), stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as e:
            self.log(f"Ошибка запуска: {e}")
        finally:
            logf.close()
        self.set_busy(True)
        self.wait_for(lambda: self.z.nfqws_running() or not self.z.manual_run_pids(), 30,
                      self.after_manual_start)

    def after_manual_start(self, ok):
        self.set_busy(False)
        self.tail_run_log()
        self.refresh_status()
        if self.running:
            self.log("✔ zapret запущен")
        else:
            self.log("✘ zapret не запустился — см. журнал выше")

    def wait_for(self, cond, timeout, done):
        """Опрашивает cond() раз в 0.5 с, затем вызывает done(bool)."""
        state = {"left": timeout * 2}

        def tick():
            self.tail_run_log()
            if cond():
                done(True)
                return False
            state["left"] -= 1
            if state["left"] <= 0:
                done(False)
                return False
            return True
        GLib.timeout_add(500, tick)

    def stop(self, on_done=None):
        mode = self.mode()
        if mode == "service":
            self.run_cmd(["service", "stop"], "Остановка сервиса", on_done=lambda c: on_done and on_done())
            return
        pids = self.z.manual_run_pids()
        if pids:
            self.log("\n▶ Остановка zapret")
            for pid in pids:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except OSError:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
            self.set_busy(True)

            def done(ok):
                self.set_busy(False)
                self.tail_run_log()
                if not ok or self.z.nfqws_running():
                    self.log("Процесс не завершился сам — выполняю service.sh kill")
                    self.run_cmd(["kill"], "Аварийная остановка",
                                 on_done=lambda c: on_done and on_done())
                    return
                self.log("✔ zapret остановлен")
                self.refresh_status()
                if on_done:
                    on_done()
            self.wait_for(lambda: not self.z.manual_run_pids(), 15, done)
            return
        self.run_cmd(["kill"], "Остановка zapret", on_done=lambda c: on_done and on_done())

    def restart(self):
        if self.mode() == "service":
            self.run_cmd(["service", "restart"], "Перезапуск сервиса")
        else:
            self.stop(on_done=lambda: GLib.timeout_add(500, lambda: self.start() and False))

    def tail_run_log(self):
        try:
            with open(RUN_LOG, errors="replace") as f:
                f.seek(self.run_log_pos)
                data = f.read()
                self.run_log_pos = f.tell()
        except OSError:
            return
        for line in data.splitlines():
            if line.strip():
                self.log(line)

    # ================================================================ service
    def on_service_action(self, _btn, act, title):
        if act == "install":
            if not self.save_form():
                return
            if self.z.manual_run_pids():
                # сервис сам перезапустит nfqws; ручной экземпляр больше не нужен
                self.stop(on_done=lambda: self.run_cmd(["service", "install"], title))
                return
        if act == "remove" and not self.confirm("Удалить сервис автозапуска?"):
            return
        self.run_cmd(["service", act], title)

    # ================================================================== lists
    def on_list_selected(self, combo):
        name = combo.get_active_id()
        if not name:
            return
        self.list_desc.set_text(dict(USER_LISTS)[name])
        path = os.path.join(self.z.user_lists_dir, name)
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            text = ""
        self.list_view.get_buffer().set_text(text)

    def save_list(self, restart):
        name = self.list_combo.get_active_id()
        if not name:
            return
        buf = self.list_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        lines = [l.strip() for l in text.splitlines()]
        text = "\n".join(l for l in lines if l) + ("\n" if any(lines) else "")
        path = os.path.join(self.z.user_lists_dir, name)
        try:
            os.makedirs(self.z.user_lists_dir, exist_ok=True)
            # пишем на месте: файл — жёсткая ссылка в zapret-latest/lists
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o644)
            link = os.path.join(self.z.repo_dir, "lists", name)
            if os.path.isdir(os.path.dirname(link)) and (
                    not os.path.exists(link) or not os.path.samefile(path, link)):
                if os.path.exists(link):
                    os.remove(link)
                os.link(path, link)
        except OSError as e:
            self.message("Не удалось сохранить список", Gtk.MessageType.ERROR, str(e))
            return
        buf.set_text(text)
        self.log(f"Список {name} сохранён")
        if restart and self.running:
            self.restart()

    def load_ipset(self):
        mode = self.z.ipset_mode() if self.z.is_valid() else None
        self.loading_ipset = True
        for m, rb in self.ipset_radios.items():
            rb.set_sensitive(mode is not None)
        if mode:
            self.ipset_radios[mode].set_active(True)
            self.ipset_note.set_text("Изменение применяется после перезапуска zapret.")
        else:
            self.ipset_note.set_text("Текущая версия стратегий не поддерживает переключение ipset.")
        self.loading_ipset = False

    def on_ipset_toggled(self, rb, mode):
        if getattr(self, "loading_ipset", False) or not rb.get_active():
            return
        try:
            self.z.set_ipset_mode(mode)
        except (OSError, RuntimeError) as e:
            self.message("Не удалось сменить режим ipset", Gtk.MessageType.ERROR, str(e))
            self.load_ipset()
            return
        self.log(f"Режим ipset: {mode}")
        if self.running and self.confirm("Режим ipset изменён. Перезапустить zapret сейчас?"):
            self.restart()

    # =============================================================== terminal
    def on_update_zapret(self):
        if not os.path.isdir(os.path.join(self.z.dir, ".git")):
            self.message("Эту папку zapret нельзя обновить автоматически.", Gtk.MessageType.ERROR,
                         secondary="Обновление работает только для копии, скачанной через git clone. "
                                   "Скачайте zapret заново на вкладке «Настройки».")
            return
        # nfqws нельзя заменить, пока он запущен: останавливаем и потом запускаем снова
        was_running = self.running
        script = ("git pull --ff-only --autostash && "
                  "./service.sh download-deps --default")
        self.run_in_terminal(["bash", "-c", script], stop_first=True,
                             on_exit=self.start if was_running else None,
                             stop_reason="На время обновления zapret будет остановлен, "
                                         "а после обновления запущен снова. Продолжить?")

    def run_in_terminal(self, argv, stop_first=False, on_exit=None, cwd=None, stop_reason=None):
        cwd = cwd or self.z.dir
        if not os.path.isdir(cwd):
            self.message("Папка zapret не найдена", Gtk.MessageType.ERROR)
            return
        if self.terminal_busy:
            self.message("В терминале уже выполняется команда.",
                         secondary="Дождитесь завершения или нажмите «Прервать».")
            self.notebook.set_current_page(3)
            return
        if stop_first and self.running:
            if not self.confirm("Zapret сейчас работает.",
                                stop_reason or "Автоподбор сам запускает и останавливает стратегии. "
                                               "Остановить текущий zapret и продолжить?"):
                return
            self.stop(on_done=lambda: self.run_in_terminal(argv, False, on_exit, cwd))
            return

        cmd = " ".join(shlex.quote(a) for a in argv)
        script = (f"{cmd}; code=$?; echo; "
                  f"echo \"=== Завершено (код $code). Окно можно закрыть или запустить другую команду ===\"")
        self.notebook.set_current_page(3)

        if self.terminal is None:
            for term in ("x-terminal-emulator", "mate-terminal", "gnome-terminal", "konsole", "xterm"):
                if shutil.which(term):
                    subprocess.Popen([term, "-e", "bash", "-c", script + "; read"], cwd=cwd)
                    return
            self.message("Не найден эмулятор терминала", Gtk.MessageType.ERROR)
            return

        self.terminal.reset(True, True)
        self.terminal_busy = True
        self.terminal_on_exit = on_exit
        self.term_label.set_text(f"Выполняется: {cmd}")
        env = [f"{k}={v}" for k, v in os.environ.items()]
        self.terminal.spawn_async(
            Vte.PtyFlags.DEFAULT, cwd, ["/bin/bash", "-c", script], env,
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, self.on_terminal_spawned, None)
        self.terminal.grab_focus()

    def on_terminal_spawned(self, _term, pid, error, *_):
        if error is not None or pid == -1:
            self.terminal_busy = False
            self.term_label.set_text(f"Ошибка запуска: {error.message if error else '?'}")
        else:
            self.terminal_pid = pid

    def on_terminal_exit(self, _term, status):
        self.terminal_busy = False
        self.term_label.set_text("Терминал свободен")
        cb, self.terminal_on_exit = self.terminal_on_exit, None
        # после скриптов могли измениться стратегии, conf.env, списки, sudoers
        self.reload_all()
        if cb:
            cb()

    def on_terminal_kill(self, *_):
        pid = getattr(self, "terminal_pid", None)
        if self.terminal_busy and pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass

    # =============================================================== settings
    def on_dir_changed(self, chooser):
        path = chooser.get_filename()
        if not path:
            return
        self.set_zapret_dir(path)

    def set_zapret_dir(self, path):
        self.z = Zapret(path)
        self.settings["zapret_dir"] = self.z.dir
        save_settings(self.settings)
        if not self.z.is_valid():
            self.message("В этой папке нет service.sh", Gtk.MessageType.WARNING,
                         "Укажите папку с клоном zapret-discord-youtube-linux.")
        self.reload_all()

    def on_clone(self, *_):
        target = os.path.expanduser("~/zapret-discord-youtube-linux")
        if os.path.exists(target):
            self.message("Папка уже существует", secondary=target)
            self.set_zapret_dir(target)
            self.dir_chooser.set_filename(target)
            return
        if not shutil.which("git"):
            self.message("Не установлен git", Gtk.MessageType.ERROR,
                         "Установите пакет git и повторите.")
            return
        url = ZAPRET_REPO_URL
        script_argv = ["bash", "-c",
                       f"git clone {shlex.quote(url)} {shlex.quote(target)} && "
                       f"cd {shlex.quote(target)} && ./service.sh download-deps --default"]

        def after():
            if os.path.isdir(target):
                self.set_zapret_dir(target)
                self.dir_chooser.set_filename(target)
                if self.z.deps_ok():
                    GLib.idle_add(self.first_run_check)
        self.run_in_terminal(script_argv, on_exit=after, cwd=os.path.expanduser("~"))

    def on_tray_toggled(self, btn):
        self.settings["close_to_tray"] = btn.get_active()
        save_settings(self.settings)

    def on_autostart_toggled(self, btn):
        try:
            if btn.get_active():
                os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
                with open(AUTOSTART_FILE, "w") as f:
                    f.write(desktop_entry("--minimized") + "X-GNOME-Autostart-enabled=true\n")
            elif os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
        except OSError as e:
            self.message("Ошибка", Gtk.MessageType.ERROR, str(e))

    def on_menu_toggled(self, btn):
        try:
            if btn.get_active():
                os.makedirs(os.path.dirname(MENU_FILE), exist_ok=True)
                with open(MENU_FILE, "w") as f:
                    f.write(desktop_entry())
            elif os.path.exists(MENU_FILE):
                os.remove(MENU_FILE)
        except OSError as e:
            self.message("Ошибка", Gtk.MessageType.ERROR, str(e))

    # ================================================================ closing
    def on_delete(self, *_):
        if self.settings.get("close_to_tray", True) and self.app.tray_available():
            self.hide()
            return True
        return not self.app.request_quit()


# =============================================================================
# Приложение + трей
# =============================================================================

class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.zapret_gui")
        self.settings = load_settings()
        self.window = None
        self.tray = None
        self.start_minimized = "--minimized" in sys.argv

    def do_startup(self):
        Gtk.Application.do_startup(self)
        prepare_sudo_wrapper()

    def do_activate(self):
        if self.window is None:
            self.build_tray()
            self.window = MainWindow(self)
            self.window.show_all()
            self.window.update_status_widgets()
            self.hold()
            if self.start_minimized:
                # дождаться, пока панель встроит иконку; без трея окно оставляем открытым
                GLib.timeout_add(1500, lambda: self.tray_available() and self.window.hide() and False)
            else:
                GLib.idle_add(self.window.first_run_check)
        else:
            self.show_window()

    def tray_available(self):
        return self.tray is not None and self.tray.is_embedded()

    def build_tray(self):
        try:
            self.tray = Gtk.StatusIcon()
        except Exception:
            self.tray = None
            return
        self.tray.set_from_pixbuf(make_icon("off", 48))
        self.tray.set_tooltip_text(APP_NAME)
        self.tray.set_title(APP_NAME)
        self.tray.connect("activate", lambda *_: self.toggle_window())
        self.tray.connect("popup-menu", self.on_tray_menu)

        self.menu = Gtk.Menu()
        self.menu_status = Gtk.MenuItem(label="Статус")
        self.menu_status.set_sensitive(False)
        self.menu_toggle = Gtk.MenuItem(label="Запустить")
        self.menu_toggle.connect("activate", lambda *_: self.window.on_toggle())
        self.menu_strats = Gtk.MenuItem(label="Стратегия")
        show = Gtk.MenuItem(label="Открыть окно")
        show.connect("activate", lambda *_: self.show_window())
        quit_item = Gtk.MenuItem(label="Выход")
        quit_item.connect("activate", lambda *_: self.request_quit())
        for it in (self.menu_status, Gtk.SeparatorMenuItem(), self.menu_toggle, self.menu_strats,
                   Gtk.SeparatorMenuItem(), show, quit_item):
            self.menu.append(it)
        self.menu.show_all()

    def rebuild_strategy_menu(self):
        sub = Gtk.Menu()
        w = self.window
        current = w.z.read_conf().get("strategy")
        group = []
        for name in w.z.strategies():
            it = Gtk.RadioMenuItem.new_with_label(group, name)
            group = it.get_group()
            it.set_active(name == current)
            it.connect("toggled", self.on_tray_strategy, name)
            sub.append(it)
        sub.show_all()
        self.menu_strats.set_submenu(sub)
        self.menu_strats.set_sensitive(bool(group) and not w.busy)

    def on_tray_strategy(self, item, name):
        if not item.get_active():
            return
        w = self.window
        w.strategy_combo.set_active_id(name)
        w.on_apply()

    def on_tray_menu(self, icon, button, time):
        self.rebuild_strategy_menu()
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)

    def update_tray(self, key, title, running):
        if self.tray is None:
            return
        self.tray.set_from_pixbuf(make_icon(key, 48))
        self.tray.set_tooltip_text(f"{APP_NAME}: {title}")
        self.menu_status.set_label(title)
        self.menu_toggle.set_label("Остановить" if running else "Запустить")
        w = self.window
        self.menu_toggle.set_sensitive(w is not None and w.z.is_valid() and w.z.deps_ok() and not w.busy)

    def toggle_window(self):
        if self.window.get_visible() and self.window.is_active():
            self.window.hide()
        else:
            self.show_window()

    def show_window(self):
        self.window.show()
        self.window.present()

    def request_quit(self):
        """Возвращает True, если приложение завершается."""
        w = self.window
        if w.z.manual_run_pids():
            dlg = Gtk.MessageDialog(
                transient_for=w if w.get_visible() else None, modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
                text="Zapret запущен из GUI вручную.")
            dlg.format_secondary_text("Остановить его перед выходом? Если оставить, он продолжит "
                                      "работать в фоне, и его можно будет остановить, снова открыв GUI.")
            dlg.add_buttons("Отмена", Gtk.ResponseType.CANCEL,
                            "Оставить работать", Gtk.ResponseType.NO,
                            "Остановить и выйти", Gtk.ResponseType.YES)
            resp = dlg.run()
            dlg.destroy()
            if resp == Gtk.ResponseType.YES:
                w.stop(on_done=self.quit_now)
                return True
            if resp != Gtk.ResponseType.NO:
                return False
        self.quit_now()
        return True

    def quit_now(self):
        if self.tray is not None:
            self.tray.set_visible(False)
        self.release()
        self.quit()


def main():
    if "--version" in sys.argv:
        print(f"zapret-gui {APP_VERSION}")
        return 0
    Gdk.set_program_class("zapret-gui")
    GLib.set_application_name(APP_NAME)
    app = App()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    argv = [a for a in sys.argv if a != "--minimized"]
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
