// Records the console's demo video: a browser walks the screens of a running
// Truejoin and the page is filmed. Run it again when a screen it shows changes.
//
//   node scripts/demo/record.js      # writes demo-out/console.webm and demo-out/scenes.json
//
// scripts/demo/README.md has the whole sequence: the sample setup it films
// (scripts/demo/config.yaml), what it needs installed, and the encoding. The
// defaults below are that setup's address and sample keys; TRUEJOIN_URL,
// TRUEJOIN_ADMIN_KEY and TRUEJOIN_TENANT_KEY point it elsewhere.
//
// Nothing secret is filmed: a key is typed into a password field, the key form
// is closed without handing a key out, and the rows are the sample database's
// made-up ones. The pointer and the caption are drawn by overlay.js, on top of
// the page, for the film only.
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const { drawOverlay } = require("./overlay");

const URL = process.env.TRUEJOIN_URL || "http://localhost:8000";
const ADMIN = process.env.TRUEJOIN_ADMIN_KEY || "demo-admin-6c2f8b15e7d3";
const TENANT = process.env.TRUEJOIN_TENANT_KEY || "demo-tenant-a-4f1c9d27b6e0";
const OUT = path.resolve(process.env.OUT || "demo-out");
const SIZE = { width: 1280, height: 720 };

const SQL = [
  "select m.ad, m.tc_kimlik, s.sehir, sum(i.tutar) as toplam",
  "from smoke.islemler i",
  "join smoke.musteriler m on m.musteri_id = i.musteri_id",
  "join smoke.subeler s on s.sube_kodu = m.sube_kodu",
  "group by 1, 2, 3 order by 4 desc",
].join("\n");
const QUESTION = "total transaction amount by city";

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: SIZE, recordVideo: { dir: OUT, size: SIZE }, colorScheme: "light", bypassCSP: true });
  const started = Date.now();
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.addInitScript(() => {
    try {
      localStorage.setItem("truejoin-console-lang", "en");
    } catch {}
  });
  await page.addInitScript(drawOverlay);

  const scenes = [];
  const scene = (name) => scenes.push({ name, at: (Date.now() - started) / 1000 });
  const say = (text) => page.evaluate((t) => window.__demo.say(t), text);
  const point = async (target, steps = 22) => {
    const locator = typeof target === "string" ? page.locator(target).first() : target;
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, 24), { steps });
    await sleep(120);
  };
  const click = async (target) => {
    await point(target);
    await page.mouse.down();
    await sleep(70);
    await page.mouse.up();
    await sleep(250);
  };
  const type = async (target, text, delay = 16) => {
    await click(target);
    await page.keyboard.type(text, { delay });
  };
  const signIn = async (key) => {
    await type("#key", key, 22);
    await click("#connect");
    await page.waitForSelector("#account:not([hidden])", { timeout: 10000 });
    await sleep(700);
  };
  // Scrolls what the caption is about up the page, clear of the caption at its foot.
  const bring = async (selector, offset = 96) => {
    await page.evaluate(([sel, off]) => {
      const at = document.querySelector(sel);
      if (at) window.scrollTo({ top: at.getBoundingClientRect().top + scrollY - off, behavior: "smooth" });
    }, [selector, offset]);
    await sleep(650);
  };
  const run = async () => {
    await click("#run");
    await page.waitForFunction(() => document.querySelector("#result table, #result .error, #result p"), null, { timeout: 15000 });
    await sleep(500);
  };

  // 0. What this is.
  await page.goto(`${URL}/ui`);
  await sleep(900);
  scene("intro");
  await say("Truejoin serves your databases to people and AI agents, under row rules, masks and an audit trail.");
  await sleep(3200);

  // 1. A tenant's key: its own rows, the national id masked.
  scene("tenant");
  await say("Sign in. This API key belongs to tenant “a”.");
  await signIn(TENANT);
  await type("#input", SQL, 9);
  await say("Plain SQL over the live database.");
  await run();
  await say("The key reads its own tenant's rows only. The national id comes back masked.");
  await point("#result th >> nth=1");
  await sleep(3800);

  // 2. A question in words.
  scene("question");
  await click(page.locator('input[name="mode"][value="question"]'));
  await page.locator("#input").fill("");
  await type("#input", QUESTION, 28);
  await say("Or ask in words. Certified business terms read the question: no model at query time.");
  await run();
  await bring("#notes");
  await sleep(1600);
  await say("The answer says how it understood the question, and shows the SQL that ran.");
  await click(page.locator("#notes summary", { hasText: "SQL" }));
  await sleep(3800);
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  await sleep(500);

  // 3. The same query as an administrator.
  scene("admin");
  await say("Now an administrator's key, and the same query.");
  await click("#account > summary");
  await click("#disconnect");
  await sleep(500);
  await signIn(ADMIN);
  await click(page.locator('input[name="mode"][value="sql"]'));
  await page.locator("#input").fill(SQL);
  await run();
  await say("Every tenant's rows this time. The mask stays: it belongs to the column, not to the query.");
  await point("#result tbody tr >> nth=0");
  await sleep(3800);

  // 4. The catalog.
  scene("catalog");
  await click("#tab-catalog");
  await page.waitForSelector("#catalogs a.cat", { timeout: 10000 });
  await say("The catalog says what the data means. A model drafts it; a person approves it before anything reads it.");
  if (!(await page.locator("#catalogs a.cat.open").count())) await click("#catalogs a.cat");
  await page.waitForSelector("#catalogs .under a.item.sub", { timeout: 10000 });
  await sleep(900);
  await click(page.locator("#catalogs .under li a", { hasText: "musteriler" }));
  await page.waitForSelector("#catalog-table table.review", { timeout: 10000 });
  await sleep(700);
  const typed = page.locator('#catalog-table input[aria-label="tc_kimlik semantic type"]');
  await point(typed);
  await say("A column's semantic type decides its mask: this one is a national id.");
  await sleep(3600);

  // 5. A key with row rules, checked before it exists.
  scene("keys");
  await page.mouse.wheel(0, -2000);
  await click("#tab-admin");
  await click("#nav-keys");
  await page.waitForSelector("#keys-actions cds-button", { timeout: 10000 });
  await say("Hand out a key with row rules.");
  await click("#keys-actions cds-button");
  await page.waitForSelector("dialog.side-panel[open]", { timeout: 5000 });
  await page.evaluate(() => window.__demo.raise());
  const panel = page.locator("dialog.side-panel[open]");
  await type(panel.locator('input[name="name"]'), "branch-analyst");
  await type(panel.locator('input[name="tenant"]'), "a");
  await click(panel.getByText("Add a rule", { exact: true }));
  await type(panel.locator('input[name="rule-table"]'), "islemler");
  await type(panel.locator('input[name="rule-column"]'), "kiraci");
  await type(panel.locator('input[name="rule-value"]'), "${tenant}");
  await click(panel.getByText("Check the rules", { exact: true }));
  await page.waitForFunction(() => document.querySelector("#keys-preview table, #keys-preview p"), null, { timeout: 15000 });
  await say("Check the rules first: the rows this key would see, beside the rows you see. No value is shown.");
  await point(panel.locator("#keys-preview"));
  await sleep(4200);
  await click(panel.locator("header button"));
  await sleep(400);

  // 6. The audit trail.
  scene("audit");
  await click("#nav-audit");
  await page.waitForSelector("#audit-body table", { timeout: 10000 });
  await bring("#audit-body table", 110);
  await say("Every read leaves an audit event: who, which tables, how many rows, what was withheld. Never values.");
  await point("#audit-body tbody tr >> nth=1");
  await sleep(4600);
  await page.evaluate(() => window.scrollTo({ top: 0 }));

  // 7. The guide.
  scene("guide");
  await click("#tab-docs");
  await page.waitForSelector("#docs-body .guide", { timeout: 10000 });
  await say("The console carries its own user guide, in English and Turkish.");
  await sleep(2600);
  await say("Open source, Apache 2.0. One process, on your own servers.");
  await sleep(2800);
  scene("end");

  const video = page.video();
  await context.close();
  const file = path.join(OUT, "console.webm");
  fs.renameSync(await video.path(), file);
  fs.writeFileSync(path.join(OUT, "scenes.json"), JSON.stringify(scenes, null, 1));
  await browser.close();
  console.log(`${file}\n${scenes.map((s) => `${s.at.toFixed(1).padStart(6)}  ${s.name}`).join("\n")}`);
  if (errors.length) {
    console.error("page errors:", errors);
    process.exit(1);
  }
})();
