-- chimera-rawmodem — minimal LuCI page to run chimera-mode from the web UI.
--
-- Install: copy to /usr/lib/lua/luci/controller/chimera.lua, then
-- `rm -f /tmp/luci-indexcache` to refresh the menu. The page appears under
-- System -> Chimera Mode (URL: /cgi-bin/luci/admin/system/chimera) and is
-- protected by the normal LuCI admin login.
--
-- Written for the LuCI shipped with the Dragino factory firmware (OpenWrt
-- Chaos Calmer 15.05.1, Lua 5.1). Deliberately renders a standalone page
-- with luci.http.write instead of a themed template: the Dragino LuCI theme
-- is customized and this keeps the page independent of it.

module("luci.controller.chimera", package.seeall)

function index()
	entry({"admin", "system", "chimera"}, call("action_chimera"), _("Chimera Mode"), 60)
end

-- action name in the URL -> literal chimera-mode arguments
local ALLOWED = {
	aprs = "aprs", tnc = "tnc", reticulum = "reticulum",
	["igatetx-on"] = "igate-tx on", ["igatetx-off"] = "igate-tx off",
}

function action_chimera()
	local http = require "luci.http"
	local sys  = require "luci.sys"
	local util = require "luci.util"

	-- Only whitelisted literals ever reach the shell.
	local mode = http.formvalue("mode")
	local result = nil
	if mode and ALLOWED[mode] then
		result = sys.exec("/usr/bin/chimera-mode " .. ALLOWED[mode] .. " 2>&1")
	end
	local status = sys.exec("/usr/bin/chimera-mode status 2>&1")

	http.prepare_content("text/html")
	http.write([[<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Chimera Mode</title>
<style>
 body { font-family: sans-serif; margin: 2em; max-width: 46em; }
 pre { background: #f4f4f4; padding: 1em; border: 1px solid #ccc; overflow-x: auto; }
 a.btn { display: inline-block; padding: .6em 1.2em; margin: 0 .4em .4em 0;
         background: #0066a2; color: #fff; text-decoration: none; border-radius: 4px; }
 a.btn:hover { background: #004d7a; }
 p.note { color: #666; }
</style></head><body>
<h2>chimera-rawmodem &mdash; operating mode</h2>]])
	if result then
		http.write("<h3>Switch output</h3><pre>" .. util.pcdata(result) .. "</pre>")
	end
	http.write("<h3>Current status</h3><pre>" .. util.pcdata(status) .. "</pre>")
	http.write([[<p>
<a class="btn" href="?mode=aprs">Switch to APRS (digi + iGate)</a>
<a class="btn" href="?mode=tnc">Switch to TNC only</a>
<a class="btn" href="?mode=reticulum">Switch to Reticulum</a>
<a class="btn" href="?">Refresh status</a>
</p>
<p>
<a class="btn" href="?mode=igatetx-on">iGate downlink ON (IS&rarr;RF)</a>
<a class="btn" href="?mode=igatetx-off">iGate downlink OFF</a>
</p>
<p class="note">Switching modes restarts the bridge and takes ~15 seconds
&mdash; the page waits and then shows the result. Clients connected to port
8001 are briefly disconnected. The selected mode persists across reboots
and power loss.</p>
<p class="note">The iGate downlink toggle (APRS-IS &rarr; RF messages for
recently heard stations, active in APRS mode with a valid passcode) only
restarts the iGate daemon &mdash; the bridge and port 8001 are not touched.
It persists too.</p>
</body></html>]])
end
