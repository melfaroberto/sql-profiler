// Bridge between the dashboard and the desktop app: native file/folder pickers for the Settings dialog.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sqlprofilerApp", {
  choosePath: kind => ipcRenderer.invoke("choose-path", kind),     // kind: "file" | "folder"
});
