// Point the console's reserved address at the mailbox that now exists.
import fs from "node:fs";

const path = "/opt/atheniq-console/data/store.json";
const store = JSON.parse(fs.readFileSync(path, "utf8"));
const learner = store.learners.find((item) => item.id === "lrn-ada");
if (!learner) throw new Error("learner lrn-ada not found");

learner.mailbox = {
  ...learner.mailbox,
  status: "active",
  mailboxId: "dd",
  activatedAt: new Date().toISOString(),
};

fs.writeFileSync(path, JSON.stringify(store, null, 2));
console.log("mailbox:", JSON.stringify(learner.mailbox));
