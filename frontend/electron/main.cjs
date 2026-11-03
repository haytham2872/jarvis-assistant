const { app, BrowserWindow, Tray, Menu, nativeImage, globalShortcut } = require('electron')
const path = require('path')

// Improve GPU compatibility on Windows — prevents GPU process crashes from
// blocklisted drivers while keeping hardware acceleration on for Three.js.
app.commandLine.appendSwitch('ignore-gpu-blocklist')
app.commandLine.appendSwitch('enable-gpu-rasterization')
// Needed on some Windows setups for backdrop-filter on transparent windows.
app.commandLine.appendSwitch('enable-features', 'VaapiVideoEncoder,VaapiVideoDecoder,CanvasOopRasterization')

const IS_DEV = process.env.NODE_ENV !== 'production'
const VITE_URL = 'http://localhost:5173'
const DIST_PATH = path.join(__dirname, '../dist/index.html')

let win = null
let tray = null

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 800,
    transparent: true,
    frame: false,
    alwaysOnTop: false,
    skipTaskbar: false,
    resizable: true,
    show: false,          // hidden by default — use Alt+J or tray to show
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  })

  if (IS_DEV) {
    win.loadURL(VITE_URL)
  } else {
    win.loadFile(DIST_PATH)
  }

  win.on('closed', () => { win = null })

  // Log renderer process crashes so errors show in the terminal even in -Prod mode.
  win.webContents.on('render-process-gone', (event, details) => {
    console.error('[JARVIS] Renderer process gone:', details.reason, details.exitCode)
  })
  win.webContents.on('did-fail-load', (event, code, desc) => {
    console.error('[JARVIS] Page failed to load:', code, desc)
  })
  win.webContents.on('console-message', (event, level, message, line, sourceId) => {
    if (level >= 2) { // 2=warning, 3=error
      console.error(`[RENDERER ${level === 3 ? 'ERR' : 'WARN'}] ${message} (${sourceId}:${line})`)
    }
  })
}

function createTray() {
  // Blank 16x16 icon fallback
  const icon = nativeImage.createEmpty()
  tray = new Tray(icon)
  tray.setToolTip('JARVIS')

  const menu = Menu.buildFromTemplate([
    { label: 'Show / Hide', click: toggleWindow },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ])
  tray.setContextMenu(menu)
  tray.on('click', toggleWindow)
}

function toggleWindow() {
  if (!win) { createWindow(); return }
  if (win.isVisible()) {
    win.hide()
  } else {
    // Open filling the screen (still resizable — user can un-maximize/drag smaller).
    win.maximize()
    win.show()
    win.focus()
  }
}

app.whenReady().then(() => {
  createWindow()
  createTray()

  globalShortcut.register('Alt+J', toggleWindow)
})

app.on('window-all-closed', (e) => e.preventDefault()) // keep tray alive
app.on('will-quit', () => globalShortcut.unregisterAll())
app.on('activate', () => { if (!win) createWindow() })
