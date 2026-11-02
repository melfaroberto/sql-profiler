// electron-builder afterPack hook (Linux only).
// Recent distributions (Ubuntu 24.04+) forbid the unprivileged user namespaces Chromium's sandbox needs, and its
// setuid fallback (chrome-sandbox) must be owned by root, which an AppImage or a tar.gz cannot provide: Electron
// then aborts at start. The flag must be on the command line (switching it from main.js is too late), so the
// executable is wrapped by a script that adds --no-sandbox. The window only shows the profiler's own local page.
const fs = require("fs");
const path = require("path");

exports.default = async function (context) {
  if (context.electronPlatformName !== "linux") return;
  const name = context.packager.executableName;
  const exe = path.join(context.appOutDir, name);
  fs.renameSync(exe, `${exe}.bin`);
  fs.writeFileSync(exe, `#!/bin/sh
# Starts ${name} without the Chromium sandbox (see build/linux-no-sandbox.js in the sources).
exec "$(dirname "$(readlink -f "$0")")/${name}.bin" --no-sandbox "$@"
`, { mode: 0o755 });
};
