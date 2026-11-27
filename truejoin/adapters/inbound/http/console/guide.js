"use strict";
// The user guide: how to use this console, read in Docs (docs.js shows it).
// Uses el and tr; says nothing the server sent.
//
// A chapter is a screen, a section a thing done there. `needs` is the
// permission /ui/access gives ("manage" is any page of Administration): a
// reader sees the chapters, sections and lines for what they may open, as the
// menu does. `covers` names the page a section is about; a page no section
// covers fails the console's tests, so a new screen arrives with its guide.
//
// A text is one sentence block, translated whole. Three marks inside it:
//   **Run**            a label on a screen, spelled as the screen spells it
//   `query`            a name to type or a value
//   [Keys](#admin/keys) a place in this console, by its address
// A label in bold has to be in the dictionary (tr.js): rename a button and the
// tests point here. Change what a screen does, and change its section with it.

const GUIDE = [
  {
    id: "guide-start",
    title: tr("Getting started"),
    intro: tr("This console is where people work with what the server serves: ask questions of the data, review what the data means, and decide who may see what. The guide follows the screens in the menu, and shows the chapters for the screens your sign-in may open."),
    sections: [
      {
        id: "sign-in",
        title: tr("Signing in"),
        body: [
          tr("There are up to three ways in. Which of them the header offers depends on how the server is set up."),
          {
            list: [
              tr("An API key: paste it in the field at the top and press **Connect**."),
              tr("**Sign in** opens your organisation's sign-in page and brings you back signed in."),
              tr("**Sign in with a password** is for a directory account, or for an account made in this console."),
            ],
          },
          tr("In a narrow window the sign-in buttons sit in a bar under the header."),
          tr("Once you are in, your name is at the header's end. Its menu has **Disconnect** and, for an account made in this console, **Change password**."),
          { note: tr("A sign-in is kept for this browser tab only: a new tab asks again.") },
        ],
      },
      {
        id: "screens",
        title: tr("The screens"),
        body: [
          {
            list: [
              { needs: "query", text: tr("[Query](#query): ask in SQL or in words, and read the answer.") },
              { needs: "read", text: tr("[Catalog](#catalog): what the tables, columns and business terms mean. A model drafts it and a reviewer approves it.") },
              { needs: "manage", text: tr("[Administration](#admin): databases, keys, users, the audit trail and settings.") },
              tr("[Docs](#docs): this guide, how an agent connects, and the REST API."),
            ],
          },
          tr("The menu shows only what your sign-in may open. An address you may not open says which permission it needs."),
          tr("Every place has an address: a table in the catalog, a section of the settings, a chapter of this guide. Copy it from the browser's address bar to send someone there. Back and Forward work as on any site."),
          tr("The language is chosen at the header's end. Light or dark follows your system's setting."),
        ],
      },
      {
        id: "access",
        title: tr("What you may see"),
        body: [
          tr("A sign-in holds permissions. `read` opens the Catalog and the REST API's reads, `query` the Query screen and the agents' tools, `update` and `create` let you review and make catalogs, `audit` opens the audit trail, and `admin` the rest of Administration."),
          tr("Beyond that, a key can carry row rules, and an approved catalog can mark a column to be masked. A query then returns only the rows the rules allow, and a masked column shows as its masking rule writes it, for example its last four characters. The answer says which columns were masked for you."),
          tr("These hold everywhere the same way: in this console, over the REST API and for an agent."),
        ],
      },
    ],
  },
  {
    id: "guide-query",
    title: tr("Asking questions"),
    needs: "query",
    intro: tr("The [Query screen](#query) answers from the connected databases. Write SQL, or ask in words once the catalog has certified business terms."),
    sections: [
      {
        id: "tables",
        title: tr("Finding the tables"),
        covers: "query",
        body: [
          tr("The side panel lists each database and its tables. Open a table to see its columns. **Query this table** writes a first query for it."),
          tr("A table marked **Loaded** is held in memory in full. The others are read from the database as a query needs them."),
          tr("What is held is read again from the database on a timer, every five minutes unless the **Refresh interval** setting says otherwise, so an answer can be that much older than the database."),
          { needs: "admin", p: tr("**Reload** on a loaded table reads it again now, rather than at the next refresh. A sign-in held to row rules is refused; masks do not stop it, and what is read again stays masked.") },
          tr("In SQL a table is named with its database, as `database.table`."),
        ],
      },
      {
        id: "sql",
        title: tr("Writing SQL"),
        body: [
          {
            steps: [
              tr("Keep **SQL** chosen above the box."),
              tr("Write a query in DuckDB's SQL. Only `SELECT` is allowed: nothing here changes the data."),
              tr("Press **Run**, or Ctrl+Enter (⌘ Enter on a Mac)."),
            ],
          },
        ],
      },
      {
        id: "question",
        title: tr("Asking in words"),
        body: [
          tr("Choose **Question** above the box and write what you want to know, for example the total of a measure by a category. The question is read with the business terms a reviewer certified in the [Catalog](#catalog), not by a model, so the same question always reads the same way."),
          tr("Above the box the screen lists the terms questions are read with. When nothing is certified yet it says so, and lists the steps that get there."),
          tr("**Follow up on the last answer** continues from the previous question: ask for another breakdown or one more condition without repeating the rest."),
          tr("When a word is not in the vocabulary, the answer names it. Someone who may edit catalogs can add the word where it belongs, right there, and the question is asked again."),
          tr("A question in a language the catalog has no words in finds nothing: the screen says which languages the approved catalogs are written in."),
        ],
      },
      {
        id: "results",
        title: tr("Reading the answer"),
        body: [
          tr("Above the rows: how many there are, how long the query took, and whether the answer came from the cache."),
          tr("For a question, **Understood as** writes the reading out as a sentence: which measure, by what, under which conditions. Check it says what you meant before you trust the numbers."),
          tr("**SQL that ran** shows the statement as it ran."),
          tr("Where the server checks joins, a join that linked the wrong columns is corrected before it runs, and the screen says what was changed. A join that could not be vouched for runs as written, with a warning."),
          tr("A column masked for you is marked **Masked** in the table's head and named above it."),
          tr("**Download CSV** saves the rows. **Chart** draws a measure and **Table** goes back to the rows."),
          tr("**Recent queries** keeps what you ran, in this browser only: pick one to bring it back. **Clear the history** empties it."),
        ],
      },
      {
        id: "refused",
        title: tr("When a query is refused"),
        body: [
          tr("A table your sign-in may not read is refused by name, and a misspelt table name gets a suggestion."),
          tr("With row rules or masks on your sign-in, a query that cannot be kept inside them is refused rather than answered wrongly. A condition on a masked column compares the masked value, not the real one."),
          { note: tr("Every read is recorded in the audit trail: who asked, which tables, how many rows. Never the values.") },
        ],
      },
    ],
  },
  {
    id: "guide-catalog",
    title: tr("Reviewing the catalog"),
    needs: "read",
    intro: tr("The [Catalog](#catalog) says what the data means: a description for every table and column, the business terms people use for them, and measures. A model writes the first draft, and nothing reads it until a reviewer approves it."),
    sections: [
      {
        id: "states",
        title: tr("Draft and approved"),
        covers: "catalog",
        body: [
          tr("A catalog is a **Draft** or **Approved**. An approved catalog is live at once: its masks, the joins the data verified and its certified terms."),
          tr("A draft changes nothing. Until a catalog is approved, it masks no column and no question is read with its terms."),
          tr("Each table has a state of its own: **Pending** until a reviewer has looked at it, **Edited** when a text was changed, **Approved** when a reviewer signed it off."),
        ],
      },
      {
        id: "make",
        title: tr("Making a catalog"),
        needs: "create",
        body: [
          {
            steps: [
              tr("Press **Analyze a database** and pick a connected database. To describe only some of its tables, name them."),
              tr("The model describes every table and column, one call per table, often a minute or more. It runs on the server: you can leave the page and come back to it."),
              tr("The result is a draft to review. Nothing is live until it is approved."),
            ],
          },
          tr("Analysing a database that already has a catalog re-opens it as a draft and keeps the edits reviewers made. Tables you name are described afresh and go back to pending; the others stay as they are."),
          tr("**Import a catalog** takes a JSON or YAML file exported from another install. It arrives as a draft with every table pending and nothing certified: what was approved elsewhere is reviewed again here. It also takes a semantic model written for another tool, an Apache Ossie model or a dbt MetricFlow one: pick which in **What the file is**. That is merged into this database's catalog as drafts, each name matched to a table or a column here and each measure checked like one written by hand, and what has no place in a catalog (a ratio of two metrics, a time spine) is listed as skipped, each with the reason."),
        ],
      },
      {
        id: "list",
        title: tr("The side list"),
        body: [
          tr("The side panel lists the catalogs. Under the open one come **Overview** and its tables, in name order. A tick marks an approved table and a tag an edited one; a table with no mark still waits."),
          tr("In a draft, **All** and **Waiting** above the tables switch between every table and the ones not yet approved."),
          tr("The search box looks in names, synonyms, tags and descriptions, across all catalogs, and lists what it finds. The side list narrows to the tables that match. Picking a row ends the search."),
          tr("**Overview** shows the catalog as a whole: a diagram of its tables and relationships, the glossary, the abbreviations and notes given to a model, saved analyses and tags."),
        ],
      },
      {
        id: "table",
        title: tr("Reviewing a table"),
        body: [
          { needs: "update", p: tr("Changing anything in a catalog needs the `update` permission.") },
          {
            steps: [
              tr("Pick a table. Read its **Description** and each column's, and correct what is wrong. A field is saved when you leave it."),
              tr("Check each column's **Semantic type**. It decides masking: masking rules are written per semantic type, so a column marked as a national id is masked wherever it appears. The field suggests the types the server knows."),
              tr("Under a text the screen says who wrote what is there now: the model, or a reviewer and when. When the last analysis changed the model's text it shows what changed, word by word, and **Use the model's text** takes the model's version."),
              tr("**Relationships** lists the joins the catalog knows, each marked as verified by the data, contradicted by it, or not checked. Only verified ones are used for joins."),
            ],
          },
        ],
      },
      {
        id: "approve",
        title: tr("Approving"),
        needs: "update",
        body: [
          tr("**Approve table and certify its terms** signs off one table together with the business term the reviewer has just read. A table with no term shows **Approve table**."),
          tr("**Approve remaining tables** approves the tables still waiting, and the catalog with them. Their terms stay uncertified that way: approve tables one by one to certify them."),
          tr("To change an approved catalog, press **Re-open for review**. Every table goes back to pending, and what is live stays live until the catalog is approved again."),
        ],
      },
      {
        id: "terms",
        title: tr("Business terms and measures"),
        body: [
          tr("A business term is what people call a table, its columns and their stored values: the words a question may use. **Synonyms** holds the words for the table, and each column has its own."),
          tr("Nothing reads a term until it is certified. **Certified** terms are what the Query screen's questions and the agents' tools use."),
          { needs: "update", p: tr("A measure is an expression over one table's columns, for example a net amount. Press **Add a measure**, write the expression (what goes inside the aggregation, not the aggregation), choose its **Aggregation** (a sum, an average, a distinct count...), **Check** it, then **Add as draft**. **Certify as measure** makes it usable. A measure certified as an average answers only as an average: summing it is refused.") },
          { needs: "update", p: tr("**Suggest measures** reads the database's query history and its views, and proposes drafts from the expressions people already use.") },
        ],
      },
      {
        id: "export",
        title: tr("Exporting"),
        body: [tr("**Export…** saves the catalog: as JSON or YAML that another install can import, as Markdown to read, or as an Apache Ossie semantic model.")],
      },
    ],
  },
  {
    id: "guide-admin",
    title: tr("Administration"),
    needs: "manage",
    intro: tr("[Administration](#admin) holds up to five pages, one per section below. What is changed there takes effect at once, without a restart."),
    sections: [
      {
        id: "databases",
        title: tr("Databases"),
        needs: "admin",
        covers: "databases",
        body: [
          {
            steps: [
              tr("On [Databases](#admin/databases), press **Add a database**."),
              tr("Give it a name (it becomes the URL segment and the SQL schema), then **Type**, **Host**, **Port**, **Database**, **User** and **Password**. **More** holds a read-only user, the pool size, the schema and SSL."),
              tr("**Test connection** tries it without saving. **Add** connects first and keeps the database only if that works."),
            ],
          },
          tr("When a connection fails, the message says what to check (the host and port, the user name or password, the database's name, a firewall, the certificate) and shows the driver's own words under it."),
          tr("A database added there is served at once: its REST routes, SQL and the agents' tools. **Remove** stops that at once and keeps its catalog."),
          tr("Databases under **From the configuration** are changed in the configuration file, not in the console."),
          { note: tr("Passwords are stored encrypted and never shown again. Leave the field blank to keep the one that is set.") },
        ],
      },
      {
        id: "keys",
        title: tr("Keys"),
        needs: "admin",
        covers: "keys",
        body: [
          {
            steps: [
              tr("On [Keys](#admin/keys), press **Hand out a key**."),
              tr("Name who or what will hold it, tick its **Permissions**, and give it a **Tenant** and an expiry if it needs them."),
              tr("Add **Row rules** to limit the rows it sees: a table, a column, an operator and a value. Every rule on a table must hold. `${tenant}` stands for the key's tenant and `${username}` for its name, so one rule serves many keys."),
              tr("**Check the rules** counts, table by table, the rows the key would see beside the rows you see. No value is shown."),
              tr("Press **Hand out**. The key is shown this once: copy it now, it cannot be shown again."),
            ],
          },
          tr("You can give only what your own key holds. A key cannot be edited: **Revoke** it and hand out a new one. A revoked key is refused from the next request on."),
          tr("Keys under **From the configuration** are changed in the configuration file, not in the console."),
        ],
      },
      {
        id: "users",
        title: tr("Users"),
        needs: "admin",
        covers: "users",
        body: [
          tr("On [Users](#admin/users), **Make a user** creates an account that signs in with a name and a password. It takes the permissions and the tenant a key takes."),
          tr("Tell them the password. They change it from the menu under their name, at the header's end."),
          tr("For each account: **Change** (what it is shown as, its permissions and tenant), **Password** (a new one, which ends whatever the account is signed in with), **Disable** (refused from the next request on; **Enable** brings it back) and **Remove**."),
          { note: tr("Accounts of a directory or of an identity provider are not listed there: they are managed where they live.") },
        ],
      },
      {
        id: "audit",
        title: tr("Audit"),
        needs: "audit",
        covers: "audit",
        body: [
          tr("[Audit](#admin/audit) has two views. **Events** lists what happened, newest first: who, what, where, the outcome, the rows returned and what was withheld (rows filtered, columns masked). It holds names only, never values."),
          tr("Narrow the events by action, key name, database and table, then press **Apply**. **Only restricted reads** keeps the reads that row rules or masks touched."),
          tr("Each sign-in is an event too: a password, a provider's sign-in, or a key or token refused, with the name given and where it came from, never the secret. Pick `sign_in` as the event to see them."),
          tr("With query fingerprints on, a query's event carries one: the same query asked with other values has the same fingerprint. **Same query** lists every time it was asked; nothing of the SQL can be read from it."),
          tr("A question put to the built-in assistant is an event too, `assistant_run`: which model, how many steps and tool calls, and how it ended, never the answer. Each call it made is a tool call of its own, made as the person who asked, and **Its calls** lists them with the reads behind them. **Request** narrows the events to one request."),
          tr("**Usage** sums up a period: which tools, tables, measures and breakdowns were asked for, and the words people asked with that the vocabulary does not know. Someone who may edit catalogs can give such a word its place from there."),
        ],
      },
      {
        id: "settings",
        title: tr("Settings"),
        needs: "admin",
        covers: "settings",
        body: [
          tr("[Settings](#admin/settings) lists every setting, section by section; the sections are in the side panel."),
          {
            steps: [
              tr("Change the values you need. Changes are kept as you move between sections, and counted as unsaved."),
              tr("Press **Save and apply**. The server rebuilds itself with the new settings and keeps serving."),
              tr("A setting that can expose data asks you to type its name before it is applied."),
            ],
          },
          tr("**Reset** puts a setting back to the file's value, or to the default. **Discard** drops what is unsaved."),
          tr("Some settings are read from the configuration file only, such as how people sign in: they are shown read-only, marked **file only**. A value written in the file locks its setting, marked **locked by the file**."),
          { note: tr("A secret is never shown. One that is set says so, and is kept when its field is left blank.") },
        ],
      },
    ],
  },
  {
    id: "guide-agents",
    title: tr("Connecting an agent"),
    needs: "query",
    intro: tr("An AI agent reads the same data over MCP, with a key, under the same permissions, row rules and masks as a person."),
    sections: [
      {
        id: "connect",
        title: tr("Where to look"),
        covers: "docs",
        body: [
          tr("[Connect](#docs/mcp-connect) has the endpoint and a client configuration to copy. [Tools](#docs/mcp-tools) lists the tools this server offers, with their arguments."),
          tr("The sections under REST API describe every route of this server, and **Try it** sends a request with your own sign-in."),
          { needs: "admin", p: tr("Give an agent a key of its own from [Keys](#admin/keys), with the `query` permission and the row rules its users need.") },
        ],
      },
    ],
  },
];

// Whether the reader may open what a chapter, section or line is about.
function guideMay(needs) {
  if (!needs) return true;
  if (needs === "manage") return ADMIN.some((name) => PAGES[name] && may(name));
  return ACCESS[needs] === true;
}

// A text's marks as nodes: **label**, `code`, [text](#address). Anything else is text.
function rich(text) {
  const out = [];
  let at = 0;
  for (const m of text.matchAll(/\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((#[^)\s]+)\)/g)) {
    if (m.index > at) out.push(text.slice(at, m.index));
    if (m[1] !== undefined) out.push(el("strong", {}, m[1]));
    else if (m[2] !== undefined) out.push(el("code", {}, m[2]));
    else out.push(el("a", { href: m[4] }, m[3]));
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

function guideBlock(item) {
  if (typeof item === "string") return el("p", {}, ...rich(item));
  if (!guideMay(item.needs)) return null;
  if (item.p) return el("p", {}, ...rich(item.p));
  if (item.note) return el("p", { class: "note" }, ...rich(item.note));
  const lines = (item.steps || item.list).filter((line) => typeof line === "string" || guideMay(line.needs));
  return el(item.steps ? "ol" : "ul", {}, ...lines.map((line) => el("li", {}, ...rich(typeof line === "string" ? line : line.text))));
}

// The chapters the reader may open, each with the sections they may.
function guideChapters() {
  return GUIDE.filter((chapter) => guideMay(chapter.needs))
    .map((chapter) => ({ ...chapter, sections: chapter.sections.filter((section) => guideMay(section.needs)) }))
    .filter((chapter) => chapter.sections.length);
}

function guideView(chapter) {
  return [
    el("h2", {}, chapter.title),
    el("p", { class: "lead" }, ...rich(chapter.intro)),
    ...chapter.sections.flatMap((section) => [
      el("h3", { id: `${chapter.id}-${section.id}`, tabindex: "-1" }, section.title),
      ...section.body.map(guideBlock).filter(Boolean),
    ]),
  ];
}
