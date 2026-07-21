# prod/agents — working agreement

Production agent code, **Python**. This tree is **canonical** — the hosted RCA
agent is built and deployed from here, in place. It is also the only version
that has run a real incident.

It doubles as Mohit's harness learning phase: the goal is that he fully
understands every line, so later harness decisions are informed ones.
**Deliberately slower is correct here.**

## One tree

Ruled 2026-07-21 (`docs/design.md` §8a-F). This reversed P11, which had put the
build in a separate TypeScript tree.

| Tree | What it is |
|---|---|
| **`prod/agents`** | **this tree.** Python, canonical, deployment target. Build here |
| `prod/ingren-agents` | **tombstone.** The TypeScript target that never got code. Four doc commits, which are where `docs/` came from. Do not build in it |
| `prod/ingren-rca` | retiring. Do not import from it or reference paths into it |

**There is no reference copy any more.** The known-good system and the thing
being changed are the same files, so **git is the reference implementation** —
and this tree has no commits yet. Getting the working system committed is
Slice 0, before anything else lands.

## Rules for Claude sessions in this tree

- **Nothing lands unexplained.** Every file written here gets a walkthrough in
  chat — what it does, why it exists, how it connects to the rest. **No batch
  code drops.** One file, or one coherent unit, per step; pause for Mohit's
  questions before moving to the next.

- **Copied files carry provenance.** One docstring line: copied from
  `<source path>` on `<date>`, plus what changed and why.

- **Read the ruling before changing the design.** Every architectural decision
  is already made and every one records what it rejected. If a slice starts
  fighting the design, that is worth raising — but re-deciding it silently is
  not. See *Design record* below.

- **Simplicity mantra.** Sole engineer. Cheapest layer that works. Add a layer
  only on a **named, observed failure**, and record the name. Where this system
  is heavier than "shell CLIs and flat files" — Postgres, SQS, Step Functions —
  each layer has a specific failure it exists to remove, written down in
  `decision.md`. That is the bar for the next one. §8a-F applied the same bar to
  a second *language* and it did not clear it either.

- **Each slice names what proves it works.** There are currently no tests
  anywhere. Don't fix that with a test-writing sprint; fix it by never landing a
  slice without saying how it was verified.

## Language

Python, everywhere in our code. Scoped to this system deliberately — the wider
ingren platform is TypeScript, and if this service is ever absorbed into it the
ruling should be re-read (§8a-F, cost 4).

**The image still needs Node.** `claude_agent_sdk` is a thin client that spawns
the `claude` Node CLI as a subprocess: `python → claude CLI (node) → bash →
python3 tools`. §8a-F buys one language in our code, not one runtime in the box.

## Database

Migrations and any write to a database are **Mohit's to run** — hand him the
exact command rather than executing it. This is a standing rule and it matters
more here than usual: the first build slice is the schema, and the agent's role
being append-only *at the database level* is a load-bearing invariant, not a
convention.

## Design record

| Document | Holds |
|---|---|
| **`docs/design.md`** | what to build, in what order, and the invariants that must not break. **§8a A–F amend or reverse the register — read it first** |
| **`docs/decision.md`** | **P1–P11** — the *why*, and what each ruling rejected. Six carry a **⚠** marker; **P11 is reversed in full** by §8a-F |
| **`docs/issues.md`** | the issue backlog for §6's slices. Drafted, not published |
| `ingren-rca/docs/plans/rca-harness/design-v2.md` | **D1–D14** — what the agent *is*. Still authoritative on the investigation itself. Move it here when ingren-rca retires |

`design.md` §4 lists seven invariants. They are the ones that fail *silently* —
breaking one produces a confidently wrong root-cause document rather than an
error. Read them before touching the record path, the tool boundary, or the
Slack ack.
