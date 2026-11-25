// Records the "with Claude" demo video: the session in claude-session.json is
// played as a terminal (replay.html), then the console's Audit page shows the
// reads that session made. See scripts/demo/README.md for the whole sequence:
// the session has to be the last thing its key did, so the audit page shows it.
//
//   node scripts/demo/record-claude.js     # writes demo-out/claude.webm and demo-out/claude.scenes.json
//
// TRUEJOIN_URL and TRUEJOIN_ADMIN_KEY as for record.js; AGENT is the name of the
// key the session ran as, and AGENT_ABOUT what the film's header says of it.
"use strict";
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright");
const { drawOverlay } = require("./overlay");

const URL = process.env.TRUEJOIN_URL || "http://localhost:8000";
const ADMIN = process.env.TRUEJOIN_ADMIN_KEY || "demo-admin-6c2f8b15e7d3";
const AGENT = process.env.AGENT || "claude-agent";
const ABOUT = process.env.AGENT_ABOUT || `the key ${AGENT} (tenant a: row rules on two tables, national ids masked)`;
const OUT = path.resolve(process.env.OUT || "demo-out");
const SIZE = { width: 1280, height: 720 };
const session = JSON.parse(fs.readFileSync(path.join(__dirname, "claude-session.json"), "utf8"));

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: SIZE, recordVideo: { dir: OUT, size: SIZE }, colorScheme: "light", bypassCSP: true });
  const started = Date.now();
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.addInitScript(drawOverlay);
  await page.addInitScript((recorded) => (window.SESSION = recorded), session);
  // The console opens signed in: the film is about the audit page, not the sign-in.
  await page.addInitScript((key) => {
    try {
      localStorage.setItem("truejoin-console-lang", "en");
      sessionStorage.setItem("truejoin-console-key", key);
    } catch {}
  }, ADMIN);

  const replay = `${pathToFileURL(path.join(__dirname, "replay.html"))}?as=${encodeURIComponent(ABOUT)}`;
  await page.goto(replay);
  const opened = (Date.now() - started) / 1000;
  const marks = await page.evaluate(() => window.played);
  const scenes = marks.map((m) => ({ name: m.name, at: Math.round((opened + m.at) * 10) / 10 }));

  await page.goto(`${URL}/ui#admin/audit`);
  await page.waitForSelector("#audit-body table", { timeout: 15000 });
  scenes[scenes.length - 1] = { name: "audit", at: (Date.now() - started) / 1000 };
  await page.evaluate((t) => window.__demo.say(t), "The same reads in Truejoin's audit trail, under the key Claude used.");
  const who = page.locator('#audit-events-bar input[name="actor"]');
  const box = await who.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 20 });
  await who.click();
  await page.keyboard.type(AGENT, { delay: 30 });
  await page.keyboard.press("Enter");
  await sleep(1200);
  await page.evaluate(() => {
    const table = document.querySelector("#audit-body table");
    window.scrollTo({ top: table.getBoundingClientRect().top + scrollY - 110, behavior: "smooth" });
  });
  await page.evaluate((t) => window.__demo.say(t), "Who asked, which tables, how many rows, what was withheld. Never the values.");
  await sleep(6500);
  scenes.push({ name: "end", at: (Date.now() - started) / 1000 });

  const video = page.video();
  await context.close();
  const file = path.join(OUT, "claude.webm");
  fs.renameSync(await video.path(), file);
  fs.writeFileSync(path.join(OUT, "claude.scenes.json"), JSON.stringify(scenes, null, 1));
  await browser.close();
  console.log(`${file}\n${scenes.map((s) => `${Number(s.at).toFixed(1).padStart(6)}  ${s.name}`).join("\n")}`);
  if (errors.length) {
    console.error("page errors:", errors);
    process.exit(1);
  }
})();
