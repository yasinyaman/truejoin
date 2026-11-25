"use strict";
// Local users: make one, change what they may do, set a password, disable, remove.
// From /auth/users, which needs the admin permission (a user changing their own password
// needs only to be signed in). Uses el, api, ask, askForm and $ from console.js and
// carbon.js. A password is typed into this page and sent once; the server keeps a hash.

let userPermissions = ["read", "query", "create", "update", "delete", "audit", "admin", "all"];
let userMinimum = 12;

function usersSay(text, error = false) {
  $("users-status").className = error ? "error" : "muted";
  $("users-status").textContent = text;
}

function userForm() {
  const form = el(
    "form",
    { class: "filters", "aria-label": tr("Make a user") },
    el("h3", {}, tr("Make a user")),
    el("label", {}, tr("Name (the sign-in name)"), el("input", { class: "field", name: "name", required: true, maxlength: 64, autocomplete: "off", placeholder: "ayse" })),
    el("label", {}, tr("Shown as"), el("input", { class: "field", name: "display", maxlength: 128, autocomplete: "off", placeholder: "Ayşe Yılmaz" })),
    el("label", {}, tr("Password"), el("input", { class: "field", name: "password", type: "password", required: true, autocomplete: "new-password" })),
    el("fieldset", { class: "modes wrap", id: "user-permissions" }, el("legend", {}, tr("Permissions"))),
    el("label", {}, tr("Tenant"), el("input", { class: "field", name: "tenant", autocomplete: "off", placeholder: tr("None") })),
    el("button", { type: "submit" }, tr("Make")),
    el("p", { class: "muted", id: "user-rule" }),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    createUser(form);
  });
  return form;
}

function drawPermissionChoices() {
  const box = $("user-permissions");
  if (!box) return;
  box.replaceChildren(
    el("legend", {}, tr("Permissions")),
    ...userPermissions.map((p) => el("label", {}, el("input", { type: "checkbox", name: "permissions", value: p, checked: p === "read" }), ` ${p}`)),
  );
  $("user-rule").textContent = tr`You can give only what you hold, and a new user carries your roles. The password is at least ${userMinimum} characters; the server keeps only a hash of it.`;
}

async function createUser(form) {
  const data = new FormData(form);
  const body = {
    name: String(data.get("name") || "").trim(),
    password: String(data.get("password") || ""),
    permissions: data.getAll("permissions").map(String),
  };
  const display = String(data.get("display") || "").trim();
  if (display) body.display = display;
  const tenant = String(data.get("tenant") || "").trim();
  if (tenant) body.tenant = tenant;
  try {
    const made = await api("POST", `${API_PREFIX}/auth/users`, body);
    form.reset();
    drawPermissionChoices();
    usersSay(tr`${made.user.name} is made. Tell them the password; they can change it under "Your password".`);
    await loadUsers(false);
  } catch (e) {
    usersSay(e.message, true);
  }
}

function day(iso) {
  return iso ? iso.slice(0, 10) : "";
}

function userState(u) {
  return u.disabled ? el("span", { class: "badge tag-red" }, tr("Disabled")) : el("span", { class: "badge status-approved" }, tr("Active"));
}

// A change an administrator asked for: the dialog collects it, the server decides.
async function changeUser(u, body, done) {
  try {
    await api("PATCH", `${API_PREFIX}/auth/users/${encodeURIComponent(u.name)}`, body);
    usersSay(done);
    await loadUsers(false);
  } catch (e) {
    usersSay(e.message, true);
  }
}

async function editUser(u) {
  let note = "";
  for (;;) {
    const entered = await askForm({
      title: tr`Change ${u.name}`,
      yes: tr("Save"),
      note,
      fields: [
        { name: "permissions", label: tr("Permissions"), checks: userPermissions, value: u.permissions },
        { name: "display", label: tr("Shown as"), value: u.display || "", required: false },
        { name: "tenant", label: tr("Tenant"), value: u.tenant || "", required: false },
      ],
    });
    if (!entered) return;
    if (!entered.permissions.length) {
      note = tr("A user needs at least one permission");
      continue;
    }
    const body = { permissions: entered.permissions, display: entered.display };
    if (entered.tenant) body.tenant = entered.tenant;
    else if (u.tenant) body.clear_tenant = true;
    try {
      await api("PATCH", `${API_PREFIX}/auth/users/${encodeURIComponent(u.name)}`, body);
      usersSay(tr`${u.name} is changed.`);
      return loadUsers(false);
    } catch (e) {
      note = e.message;
    }
  }
}

async function resetPassword(u) {
  let note = "";
  for (;;) {
    const entered = await askForm({
      title: tr`New password for ${u.name}`,
      yes: tr("Set password"),
      note,
      help: tr`At least ${userMinimum} characters. Whatever ${u.name} is signed in with ends.`,
      fields: [
        { name: "password", label: tr("New password"), type: "password", autocomplete: "new-password", minlength: userMinimum },
        { name: "again", label: tr("New password again"), type: "password", autocomplete: "new-password" },
      ],
    });
    if (!entered) return;
    if (entered.password !== entered.again) {
      note = tr("The two passwords differ");
      continue;
    }
    try {
      await api("POST", `${API_PREFIX}/auth/users/${encodeURIComponent(u.name)}/password`, { password: entered.password });
      usersSay(tr`The password of ${u.name} is changed.`);
      return loadUsers(false);
    } catch (e) {
      note = e.message;
    }
  }
}

function userRow(u) {
  const act = (label, run) => {
    const b = el("button", { type: "button", class: "secondary small" }, label);
    b.addEventListener("click", run);
    return b;
  };
  const toggle = u.disabled
    ? act(tr("Enable"), () => changeUser(u, { disabled: false }, tr`${u.name} can sign in again.`))
    : act(tr("Disable"), async () => {
        if (await ask(tr`${u.name} is refused from the next request on, and the password stays. You can enable them again.`, { title: tr`Disable ${u.name}?`, yes: tr("Disable") }))
          changeUser(u, { disabled: true }, tr`${u.name} is disabled.`);
      });
  const remove = act(tr("Remove"), async () => {
    if (!(await ask(tr`${u.name} is refused from the next request on, and the account is gone. This cannot be undone.`, { title: tr`Remove ${u.name}?`, yes: tr("Remove") }))) return;
    try {
      await api("DELETE", `${API_PREFIX}/auth/users/${encodeURIComponent(u.name)}`);
      usersSay(tr`${u.name} is removed.`);
      await loadUsers(false);
    } catch (e) {
      usersSay(e.message, true);
    }
  });
  return el(
    "tr",
    {},
    el("td", {}, u.name, el("div", { class: "muted" }, u.display || "")),
    el("td", {}, userState(u)),
    el("td", {}, u.permissions.join(", ")),
    el("td", {}, [u.tenant ? tr`tenant ${u.tenant}` : "", (u.roles || []).join(", ")].filter(Boolean).join(" · ")),
    el("td", {}, u.last_sign_in ? day(u.last_sign_in) : tr("Never")),
    el("td", {}, day(u.created_at), el("div", { class: "muted" }, u.created_by || "")),
    el("td", { class: "actions" }, act(tr("Change"), () => editUser(u)), act(tr("Password"), () => resetPassword(u)), toggle, remove),
  );
}

// The signed-in user's own password: they need only know the current one.
function selfForm() {
  const form = el(
    "form",
    { class: "filters", "aria-label": tr("Your password") },
    el("h3", {}, tr("Your password")),
    el("label", {}, tr("Current password"), el("input", { class: "field", name: "current", type: "password", required: true, autocomplete: "current-password" })),
    el("label", {}, tr("New password"), el("input", { class: "field", name: "next", type: "password", required: true, autocomplete: "new-password" })),
    el("label", {}, tr("New password again"), el("input", { class: "field", name: "again", type: "password", required: true, autocomplete: "new-password" })),
    el("button", { type: "submit", class: "secondary" }, tr("Change my password")),
    el("p", { class: "muted" }, tr("For an account made here. A directory account changes its password at the directory.")),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = new FormData(form);
    if (data.get("next") !== data.get("again")) return usersSay(tr("The two passwords differ"), true);
    try {
      await api("PUT", `${API_PREFIX}/auth/users/me/password`, { current_password: String(data.get("current")), new_password: String(data.get("next")) });
      form.reset();
      // The sign-in this page held ended with the old password: say so and let go of it.
      $("key").value = "";
      remember("");
      keepSession(null);
      await whoAmI();
      usersSay(tr("Your password is changed. Sign in again with the new one."));
    } catch (e2) {
      usersSay(e2.message, true);
    }
  });
  return form;
}

function usersTable(rows) {
  return el(
    "div",
    { class: "scroll" },
    el(
      "table",
      { class: "review" },
      el("thead", {}, el("tr", {}, ...[tr("Name"), tr("State"), tr("Permissions"), tr("Tenant · roles"), tr("Last sign-in"), tr("Made"), ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...rows),
    ),
  );
}

async function loadUsers(clearStatus = true) {
  if (clearStatus) usersSay(tr("Loading…"));
  $("users-self").replaceChildren(selfForm());
  try {
    const data = await api("GET", `${API_PREFIX}/auth/users`);
    userPermissions = data.permissions || userPermissions;
    userMinimum = data.min_password_length || userMinimum;
    $("users-filters").hidden = !data.available;
    drawPermissionChoices();
    $("users-body").replaceChildren(
      el("h3", {}, tr("Local users")),
      !data.available
        ? el("p", { class: "muted" }, data.detail)
        : data.users.length
          ? usersTable(data.users.map(userRow))
          : el("p", { class: "muted" }, tr("No local user yet.")),
    );
    if (clearStatus) usersSay(data.auth_enabled === false ? tr("Authentication is off: no user is checked until it is turned on.") : "");
  } catch (e) {
    $("users-filters").hidden = true;
    $("users-body").replaceChildren();
    usersSay(e.status === 403 ? tr("This account cannot manage users: that needs the admin permission.") : e.message, true);
  }
}

$("users-filters").replaceChildren(userForm());
drawPermissionChoices();
page("users", { open: () => loadUsers(), reload: () => loadUsers() });
