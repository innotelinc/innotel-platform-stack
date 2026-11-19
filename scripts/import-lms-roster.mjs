// Load the learners the institute actually has into the console's store.
//
// Two sources, because the estate has two kinds of truth:
//   * `--lms` (default) reads Open edX's `auth_user` through tutor's MySQL.
//   * `--csv <path>` reads a registrar export with `name,email[,program]`.
//
// Without `--apply` it only reports what it would do, which is the mode to run
// first: the LMS demonstrably holds placeholder accounts (`student.one`), and
// overwriting four named records with those would be a downgrade.
//
// Matching is by lowercased e-mail. An existing learner keeps its id, mailbox,
// enrolments and completions; only name/program/username are refreshed. New
// people get `lrn-<local part>` unless that id is taken.
import fs from "node:fs";
import { execFileSync } from "node:child_process";

const args = process.argv.slice(2);
const apply = args.includes("--apply");
const csvPath = args.includes("--csv") ? args[args.indexOf("--csv") + 1] : null;
const STORE = process.env.CONSOLE_STORE || "/opt/atheniq-console/data/store.json";

function fromLms() {
  const sql = [
    "SELECT u.username, u.email, u.first_name, u.last_name",
    "FROM auth_user u",
    "WHERE u.is_active = 1 AND u.is_staff = 0 AND u.is_superuser = 0",
    "ORDER BY u.id",
  ].join(" ");
  const out = execFileSync(
    "docker",
    ["exec", "tutor_local-mysql-1", "sh", "-c",
      `mysql -uroot -p"$MYSQL_ROOT_PASSWORD" openedx -N -B -e ${JSON.stringify(sql)} 2>/dev/null`],
    { encoding: "utf8" },
  );
  return out
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => {
      const [username, email, first, last] = line.split("\t");
      const name = [first, last].filter(Boolean).join(" ").trim() || username;
      return { username, email, name };
    });
}

function fromCsv(path) {
  const rows = fs.readFileSync(path, "utf8").split(/\r?\n/).filter((line) => line.trim());
  const header = rows.shift().split(",").map((cell) => cell.trim().toLowerCase());
  return rows.map((line) => {
    const cells = line.split(",").map((cell) => cell.trim());
    const at = (key) => cells[header.indexOf(key)] ?? "";
    const name = at("name");
    return {
      username: at("username") || name.toLowerCase().replace(/[^a-z0-9]+/g, ".").replace(/^\.|\.$/g, ""),
      email: at("email"),
      name,
      programId: at("program") || undefined,
    };
  });
}

// Open edX keeps service accounts in the same table as people. Enrolling
// `cms@openedx` as a student is worse than importing nothing, so the filter is
// explicit about what a person is rather than relying on `is_staff` alone.
const SYSTEM_DOMAIN = /@(fake\.email|openedx|localhost|example\.(com|org|edu))$/i;
const SYSTEM_USER = /^(cms|admin|service|robot|bot|login_service_user)$|^student\.[a-z]+$/i;

function notAPerson(row) {
  const local = String(row.email).split("@")[0];
  if (SYSTEM_DOMAIN.test(row.email)) return "system mailbox";
  if (SYSTEM_USER.test(row.username || local)) return "service or placeholder account";
  if (!row.name || row.name === (row.username || local)) return "no name to enrol";
  return null;
}

const incoming = (csvPath ? fromCsv(csvPath) : fromLms()).filter((row) => row.email && row.name);
const store = JSON.parse(fs.readFileSync(STORE, "utf8"));
const byEmail = new Map(store.learners.map((learner) => [String(learner.email).toLowerCase(), learner]));
const takenIds = new Set(store.learners.map((learner) => learner.id));

const added = [];
const updated = [];
const skipped = [];

for (const row of incoming) {
  const key = row.email.toLowerCase();
  const local = key.split("@")[0];
  // A placeholder account is not a learner; importing it would make the roster worse.
  const why = notAPerson(row);
  if (why) {
    skipped.push(`${row.email} (${why})`);
    continue;
  }
  const existing = byEmail.get(key);
  if (existing) {
    const changes = {};
    if (row.name && existing.name !== row.name) changes.name = row.name;
    if (row.programId && existing.programId !== row.programId) changes.programId = row.programId;
    if (Object.keys(changes).length) updated.push({ id: existing.id, changes });
    continue;
  }
  let id = `lrn-${local.replace(/[^a-z0-9]+/g, "-")}`;
  if (takenIds.has(id)) id = `${id}-${takenIds.size + 1}`;
  takenIds.add(id);
  added.push({
    id,
    username: row.username,
    name: row.name,
    email: row.email,
    ...(row.programId ? { programId: row.programId } : {}),
    enrolledAt: new Date().toISOString(),
  });
}

console.log(`source: ${csvPath ? "csv " + csvPath : "Open edX auth_user"}`);
console.log(`read ${incoming.length}, add ${added.length}, update ${updated.length}, skip ${skipped.length}`);
for (const row of added) console.log("  +", row.id, row.email, "|", row.name);
for (const row of updated) console.log("  ~", row.id, JSON.stringify(row.changes));
for (const row of skipped) console.log("  -", row);

if (!apply) {
  console.log("\ndry run: nothing written. Re-run with --apply to merge these into", STORE);
  process.exit(0);
}
if (!added.length && !updated.length) {
  console.log("\nnothing to change; store untouched");
  process.exit(0);
}

fs.copyFileSync(STORE, `${STORE}.bak-${Date.now()}`);
store.learners.push(...added);
for (const { id, changes } of updated) {
  Object.assign(store.learners.find((learner) => learner.id === id), changes);
}
fs.writeFileSync(STORE, JSON.stringify(store, null, 2));
console.log(`\nwrote ${STORE}: ${store.learners.length} learners`);
