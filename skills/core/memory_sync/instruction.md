# Memory Sync: Memory & Diary Manager

Maria's protocol for extracting and saving session memories and maintaining her active context.

---

## 📂 Core Storage Architecture

Memory is stored in a single unified JSON database:

*   **`Memory.json`** — Located at `PROJECT_ROOT/Brain/Memory.json`. It contains structured, parameter-based facts that are dynamically loaded and formatted into Maria's prompt during synchronization.

---

## 🧠 Memory Schema & Cognitive Parameters

Each memory entry in `Memory.json` must be stored as an object inside the `memories` array with the following fields:

*   **`id`**: Unique string identifier (e.g., `mem_021`).
*   **`tier`**: The operational level determining decay speed and prominence:
    *   `T1` — Core Identity & Setup (0% decay). Name, OS, configs, safety rules.
    *   `T2` — Technical & Active Projects (15% decay/day). Coding tasks, active system tweaks.
    *   `T3` — Ephemeral / Banter / Soft Context (50%–70% decay/day). Trivial preferences, minor jokes, temporary moods.
*   **`content`**: The factual, concise memory payload.
*   **`strength`**: Float `[0.0 to 1.0]`. Reinforcing an existing memory resets/raises strength. Decays when not mentioned.
*   **`importance` / `weight`**: Float `[0.0 to 1.0]`. Indicates operational relevance. Technical or milestone achievements receive a boost.
*   **`created_cycle`**: The cycle number (or date) the memory was first recorded.
*   **`last_updated_cycle`**: The cycle number (or date) the memory was last updated/reinforced.

---

## ⚡ Execution Protocol

When Sifat asks to update memory, end the day, or sync:

### Step 1: Extract Memory
*   Analyze the current day's session logs (or the specific date's sessions requested by Sifat).
*   Extract new wins, technical decisions, config changes, academic milestones, or important banter.
*   Decay old memories: decrease the `strength` of existing `T2` and `T3` memories that weren't reinforced or mentioned. Purge any that drop below `0.1`.

### Step 2: Update `Memory.json`
*   Add new memories or reinforce existing ones by adjusting their `strength`, `importance`, and `last_updated_cycle`.
*   Maintain clean JSON structure.

### Step 3: Verify & Confirm
*   Verify that `Memory.json` is written atomically and parses correctly.
*   Confirm the update with Sifat. The startup logic will automatically format and upload the new memory structure to Qwen custom instructions on the next session run.


KEEP THE MEMORY SMALL, REMOVE THE OLD AND UNIMPORTANT MEMORY. KEEP EVERY MEMORY PART DENSE AND COMPACT WITHOUT ANY UNNECESSARY INFORMATION.