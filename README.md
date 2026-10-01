# Zapret-GUI

🖥️ Графический интерфейс для обхода замедления YouTube и Discord на Linux на базе [zapret-discord-youtube-linux](https://github.com/Sergeydigl3/zapret-discord-youtube-linux).

## ✨ О проекте

**Zapret-GUI** — это удобная GUI-оболочка (Python + GTK3) для [zapret-discord-youtube-linux](https://github.com/Sergeydigl3/zapret-discord-youtube-linux) от Sergeydigl3. Тот, в свою очередь, использует стратегии [Flowseal](https://github.com/Flowseal/zapret-discord-youtube) и ядро [zapret](https://github.com/bol-van/zapret) (nfqws) от bol-van.

Вместо командной строки — понятное окно: выбор стратегии, кнопка «Включить», автозапуск и значок в трее. GUI не дублирует логику zapret, а вызывает его `service.sh`.

## 🚀 Быстрый старт

Скачайте пакет для своей системы со страницы **[Releases](../../releases/latest)** и установите его (или откройте двойным щелчком). Затем запустите **Zapret GUI** из меню приложений — мастер первого запуска сам скачает zapret и настроит права.

Запуск без установки:

```bash
git clone https://github.com/Mechtaatel/zapret-gui.git
cd zapret-gui

# Запустить GUI
python3 zapret_gui.py
```

## 🎯 Возможности

- 🖱️ **Минималистичный GUI** — управление без терминала
- 🧙 **Мастер первого запуска** — скачивает zapret и настраивает sudo без пароля
- ⚡ **Быстрый запуск** — выбор стратегии стрелками ← →, включение одной кнопкой
- 🔧 **Гибкие настройки** — интерфейс, GameFilter TCP/UDP, бэкенд файрвола, режимы ipset
- 📝 **Редактор списков** — свои домены для обхода и исключения
- 🔄 **Автоматический подбор** — автоподбор стратегий во встроенном терминале
- 📌 **Значок в трее** — цвет показывает состояние, из меню можно включить/выключить и сменить стратегию
- 🛡️ **Автозапуск** — системный сервис (systemd, OpenRC, runit, s6, dinit) и запуск свёрнутым в трей
- 📦 **Готовые пакеты** — .deb, .rpm и AUR

## 💻 Требования

- **Python 3.8+** и **GTK 3**
- **nftables** (или iptables)
- git, curl, sudo
- Желательно: **VTE 2.91** (встроенный терминал) и **zenity** (окно ввода пароля)
- Права администратора (sudo)

При установке из пакета зависимости ставятся автоматически.

## 📖 Использование

### Главное окно
Запустите **Zapret GUI** из меню приложений (раздел «Интернет» / «Сеть»). Вкладки:
1. **Управление** — выбрать стратегию и интерфейс, включить/выключить, посмотреть журнал
2. **Автозапуск** — установка/удаление системного сервиса
3. **Списки** — свои домены и исключения, режим ipset
4. **Инструменты** — автоподбор стратегий, обновление стратегий и nfqws, настройка sudo
5. **Настройки** — параметры самого GUI

Если стратегия не помогает — переключайте её стрелками **← →** или запустите **«Инструменты → Автоподбор для YouTube»**.

### Основные команды
```bash
# Запустить GUI
zapret-gui

# Запустить свёрнутым в трей
zapret-gui --minimized

# Показать версию
zapret-gui --version
```

## 📦 Установка через пакетные менеджеры

### Debian / Ubuntu / Linux Mint
```bash
sudo apt install ./zapret-gui_*_all.deb
```

### Fedora
```bash
sudo dnf install ./zapret-gui-*.noarch.rpm
```

### openSUSE
```bash
sudo zypper install --allow-unsigned-rpm ./zapret-gui-*.noarch.rpm
```

### Arch Linux (AUR)
```bash
yay -S zapret-gui        # стабильный релиз
yay -S zapret-gui-git    # свежий код из main
```

Или вручную:
```bash
git clone https://aur.archlinux.org/zapret-gui.git
cd zapret-gui
makepkg -si
```

## 🔗 Источники

Проект построен на основе:

- **[Sergeydigl3/zapret-discord-youtube-linux](https://github.com/Sergeydigl3/zapret-discord-youtube-linux)** — адаптер zapret для Linux, который управляется через GUI
- **[Flowseal/zapret-discord-youtube](https://github.com/Flowseal/zapret-discord-youtube)** — стратегии и конфигурации обхода блокировок
- **[bol-van/zapret](https://github.com/bol-van/zapret)** — основной инструмент для обхода DPI (nfqws)

## ⚠️ Важно

- Требуются права администратора для работы с nftables и nfqws
- После «Настроить вход без пароля» sudo не спрашивает пароль только для `nft`, `iptables` и `nfqws`
- В GNOME для значка в трее нужно расширение [AppIndicator Support](https://extensions.gnome.org/extension/615/appindicator-support/)
- VPN и прокси-клиенты могут конфликтовать с zapret
- Это не VPN — это инструмент обхода DPI
- Использование в соответствии с местным законодательством

## 📋 Лицензия

MIT

## 🤝 Контрибьюторы

Благодарности:
- **Sergeydigl3** — за адаптацию zapret для Linux
- **Flowseal** — за стратегии обхода блокировок
- **bol-van** — за основную архитектуру zapret
- Сообществу Linux за поддержку

## 📧 Поддержка

- 📢 [Telegram Channel](https://t.me/your_channel)
- 💬 [Telegram Chat](https://t.me/your_chat)
- 🔗 [Issues](../../issues) — сообщайте об ошибках
- 🔀 [Pull Requests](../../pulls) — предложения улучшений приветствуются!

## 📊 Статистика

- **Поддерживаемые ОС:** Debian, Ubuntu, Linux Mint, Fedora, openSUSE, Arch Linux, Manjaro и др.
- **Окружения рабочего стола:** GNOME, KDE, XFCE, Cinnamon и др.
- **Архитектуры:** пакет noarch (Python); nfqws — в зависимости от zapret
- **Версия:** 1.1.0
- **Статус:** Активно разрабатывается

---

**Примечание:** Это независимая реализация GUI-интерфейса для управления zapret на Linux. Убедитесь, что используете инструмент ответственно и в соответствии с местным законодательством.
