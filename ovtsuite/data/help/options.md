---
title: Options
order: 22
context: OptionsDialog
---
# Options

**Tools > Options...** (Ctrl+,) holds the settings for the whole app: how it
starts, how projects are kept safe, the theme, the toolbar's buttons and the
keyboard shortcuts. Changes take effect when you click **OK** or **Apply**,
in every open window at once; **Cancel** leaves everything as it was.
**Restore Defaults** puts the page on screen back as it was when the app was
installed.

![Tools > Options, the General page](images/options-general.png)

The settings are kept for your Windows user. The standalone flasher, logger
and sniffer use them too (the theme, for example).

## General

| Setting | What it does |
|---|---|
| **At start-up, last session's projects** | **Ask which to reopen** (the default) lists the projects that were open when the app last closed. **Reopen them all** opens them without asking. **Start empty** opens none. |
| **Remember the window's size and position** | Reopen the window at the size and place it had. When this is off, the window always opens maximized. |
| **Show file information when a file opens** | The **File information** window after an import. The same switch is in that window. |
| **Ask for the vehicle details of a new project** | The **Project information** prompt for a new project. |
| **Recent files listed** | How many projects **File > Open Recent** keeps. |
| **Closed projects kept** | How many projects **File > Restore project from history** keeps. |

## Projects and saving

| Setting | What it does |
|---|---|
| **Keep unsaved work for crash recovery every** | How often unsaved changes are put aside (see [Recovering after a crash](projects.md#recovering-after-a-crash)). |
| **Notice when another program changes an open project's file** | Watches the open projects' files while you work (see [When another program changes the project file](projects.md#when-another-program-changes-the-project-file)). When it is off, the app still checks before every save and never writes over another program's changes without asking. |
| **Reload a project with nothing unsaved when its file changes** | On: such a project reloads by itself. Off: the app asks first. |
| **Look at the open projects' files every** | How often the watch looks. |

## Editor

| Setting | What it does |
|---|---|
| **Versions listed in Compare with saved version** | How many saves the menu lists before **Version history...**. |

## Appearance

| Setting | What it does |
|---|---|
| **Theme** | **System** follows Windows' light or dark app mode. **Light** and **Dark** stay light or dark whatever Windows is set to. The change applies at once. |
| **Show icons in menus** | Whether the menus show the toolbar's icons beside their items. |
| **Scroll bars** | **As Windows is set** follows Windows 11's *Always show scrollbars* setting. **Always show** shows them in the app whatever that setting is. Takes effect the next time the app starts. |

## Toolbar

![The toolbar as it comes](images/main-toolbar.png)

The toolbar under the menus holds buttons for menu items. Each button is
the menu item itself: it is greyed out, ticked and triggered exactly as the
menu item is, and its tooltip names its shortcut. A button for a sub-menu,
such as **Open Recent** or **Export**, drops that menu down. One button is
not a menu item: **Find panel** (under Edit) shows and hides the Find panel,
where **Edit > Find...** only opens it.

- **Show the toolbar**, **Icon size** (small, medium, large) and **Buttons
  show** (icons only, or text beside or under the icon).
- **Buttons:** every menu item is listed on the left, by menu. Pick one and
  click **Add ▶** (or double-click it) to put it after the selected button on
  the right. **◀ Remove** (or a double-click on the right) takes a button
  off. **Separator** adds a divider. Drag the buttons, or use **Up** and
  **Down**, to reorder them.

![Options > Toolbar: menu items on the left, the toolbar's buttons on the right](images/options-toolbar.png)

Right-click the toolbar for **Customize toolbar...** (this page) and **Hide
toolbar**. **View > Toolbar** shows or hides it too.

## Keyboard shortcuts

Every menu item is listed by menu with its shortcut and its default. A
changed shortcut is shown in **bold**.

1. Pick a menu item. Type in **Filter** to find it by name or key.
2. Click in **Shortcut** and press the keys you want.
3. **None** removes the item's shortcut. **Default** puts the app's own
   back.

![Options > Keyboard shortcuts, Save As given Ctrl+Shift+S](images/options-shortcuts.png)

Two items can't share a key: both are shown in red, with a note saying
which, and **OK** / **Apply** wait until one of them changes. The new
shortcuts work as soon as you click **OK**, and the menus and toolbar
tooltips show them. The [Keyboard shortcuts](editor/keyboard-shortcuts.md)
page lists the app's defaults.

## Modules and AI

**Allow unsigned modules** is the same switch as in **Tools > Modules...**
(see [Modules](modules.md)). It is only for modules you build yourself.
**Module manager...** opens that window. The AI assistant's settings have a
window of their own: **AI settings...** opens it (see [AI settings and
privacy](ai/settings.md)).

## Advanced

- **Export...** writes your options, toolbar and shortcuts to a file.
  **Import...** reads them back, for a backup or another computer.
- **Reset all options...** puts every option, the toolbar and the shortcuts
  back to their defaults. Recent files, open tabs and saved connections are
  kept.
