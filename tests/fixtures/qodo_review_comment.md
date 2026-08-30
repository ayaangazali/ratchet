<h3>Code Review by Qodo</h3>

<code>🐞 Bugs (8)</code>  <code>📘 Rule violations (4)</code>  <code>📜 Skill insights (0)</code>

<img src="https://www.qodo.ai/wp-content/uploads/2025/11/light-grey-line.svg" height="10%" alt="Grey Divider">

<br/>

<img src="https://img.shields.io/badge/High-634FD1?style=flat-square" height="20px" alt="Action required">

<details>
<summary>  1.  <s>Chat commits unrelated changes</s> <code>✓ Resolved</code> <code>🐞 Bug</code> <code>≡ Correctness</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
><b><i>_commit_and_finish</i></b> invokes <b><i>_commit</i></b> even when every model file was refused or its diff failed to
>apply, and <b><i>_commit</i></b> stages the entire repository with <b><i>git add -A</i></b>, including modified or untracked
>files that predated the turn. The unsuccessful turn can therefore commit unrelated user work, which
><b><i>/undo</i></b> may subsequently revert along with the chat commit.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/chat.py[R151-153]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-5282c92504ec5ccf7f520f6dee7f6227144b4fe63e9fe2dff845ef30d79d4971R151-R153)</code>
>
>```diff
>+    def _commit_and_finish(self, turn: Turn, t0: float) -> Turn:
>+        if turn.files or not turn.error:
>+            sha = self._commit(turn.intent or "chat turn")
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>Refused files are skipped and failed diffs only emit a note without setting <b><i>turn.error</i></b>, so
><b><i>_commit_and_finish</i></b> still calls <b><i>_commit</i></b>; although the chat writer records its output, it
>establishes no baseline for existing changes, and <b><i>_commit</i></b> stages all repository paths except
><b><i>.ratchet</i></b> using <b><i>git add -A</i></b>. Because the TUI&#x27;s undo command runs <b><i>git revert HEAD</i></b> on the latest
>chat commit, any unrelated pre-existing files swept into that commit are also included in the
>revert.
></pre>
>
> <code>[ratchet/chat.py[103-126]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/chat.py/#L103-L126)</code>
> <code>[ratchet/chat.py[151-170]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/chat.py/#L151-L170)</code>
> <code>[ratchet/chat.py[151-171]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/chat.py/#L151-L171)</code>
> <code>[ratchet/tui/app.py[897-914]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/tui/app.py/#L897-L914)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>
>A chat turn stages all repository changes rather than only files successfully changed by that turn. Consequently, even a turn where every file was refused or every diff failed to apply can commit unrelated pre-existing modified or untracked user files, and `/undo` can later revert that unrelated work with the chat commit.
>
>## Issue Context
>
>The turn tracks written file paths but does not snapshot or isolate the initial working tree. Refused files are skipped and failed diffs do not set `turn.error`, allowing commit processing to continue; track successfully written or applied paths, stage only those paths, and do not create a commit when the turn produced no changes. The TUI undo flow reverts the entire latest chat commit, so preventing unrelated files from entering that commit is necessary to keep undo scoped to the chat turn.
>
>## Fix Focus Areas
>
>- ratchet/chat.py[103-126]
>- ratchet/chat.py[151-171]
>- ratchet/tui/app.py[897-914]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  2.  Cancelled turns can resume <code>🐞 Bug</code> <code>☼ Reliability</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
><b><i>action_deny</i></b> calls <b><i>Worker.cancel()</i></b> on a threaded worker and then allows a new turn once that
>worker is no longer marked running, but cancelling a Textual thread does not stop its function. The
>new turn clears the shared cancellation event, so the old provider call can return later, observe a
>cleared flag, and apply stale output concurrently with the new turn.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/tui/app.py[R974-977]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-84e82b5444b5506723dc7fc5cd88d9ba0fac69e58acde86eeda61edd665f37eaR974-R977)</code>
>
>```diff
>+        if self._chat_worker is not None and self._chat_worker.is_running:
>+            self._chat_session().cancel.set()
>+            self._chat_worker.cancel()
>+            self._note(self.query_one("#activity", RichLog), "interrupt requested", m.AMBER)
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>The implementation uses one <b><i>threading.Event</i></b>, clears it at the start of every turn, and cancels the
>Textual thread worker. Textual&#x27;s worker documentation states that threads cannot be cancelled like
>coroutines and must manually check cancellation state.
></pre>
>
> <code>[ratchet/chat.py[72-90]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/chat.py/#L72-L90)</code>
> <code>[ratchet/tui/app.py[916-927]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/tui/app.py/#L916-L927)</code>
> <code>[ratchet/tui/app.py[968-977]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/tui/app.py/#L968-L977)</code>
> <code>🌐 [Textual documents that thread workers cannot be cancelled like coroutines and must manually check whether the worker was cancelled.](https://textual.textualize.io/guide/workers/)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>Cancelling a threaded Textual worker does not terminate its thread, and the shared event can be cleared by a subsequent turn.
>
>## Issue Context
>Use a per-turn cancellation token or generation ID, and do not permit a replacement turn to make an older thread eligible to write.
>
>## Fix Focus Areas
>- ratchet/chat.py[72-90]
>- ratchet/tui/app.py[916-927]
>- ratchet/tui/app.py[968-977]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  3.  Harness snapshots break graph <code>🐞 Bug</code> <code>≡ Correctness</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>For harness sandboxes, <b><i>GraphRun._run_node</i></b> stores the provider snapshot reference as the node
>commit, then <b><i>_advance</i></b> passes that remote reference to local <b><i>git checkout</i></b>. A green graph node
>therefore raises <b><i>GitError</i></b> instead of advancing whenever the harness snapshot is not a local Git
>object.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/objective.py[R453-454]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-7f8ba6fc9c4b79a8d78a80d4e892cbe443fbf2f78eda2c4c8ef6bef9b2129be8R453-R454)</code>
>
>```diff
>+                        sha = sb.snapshot()
>+                    self._advance(node, sha)
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
><b><i>HarnessSandbox.snapshot()</i></b> explicitly returns whatever restorable reference the provider supplies,
>while <b><i>_advance</i></b> unconditionally invokes a local Git checkout that requires a locally resolvable
>revision.
></pre>
>
> <code>[ratchet/objective.py[446-461]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/objective.py/#L446-L461)</code>
> <code>[ratchet/objective.py[505-509]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/objective.py/#L505-L509)</code>
> <code>[ratchet/sandbox.py[194-232]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/sandbox.py/#L194-L232)</code>
> <code>[ratchet/gitstate.py[29-33]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/gitstate.py/#L29-L33)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>Harness snapshot references are being treated as local Git commits, so graph execution fails after a green harness-backed node.
>
>## Issue Context
>Preserve distinct local-commit and remote-snapshot state, and advance each provider using the state representation it supports.
>
>## Fix Focus Areas
>- ratchet/objective.py[446-461]
>- ratchet/objective.py[505-509]
>- ratchet/sandbox.py[194-232]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details><summary><ins><strong>View high (5)</strong></ins></summary><br/>
<details>
<summary>  4.  Keep widget access on UI thread <code>🐞 Bug</code> <code>☼ Reliability</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
><b><i>_run_chat_turn</i></b> runs in a threaded Textual worker but queries widgets and creates/reads session
>state directly before marshaling only later UI writes with <b><i>call_from_thread</i></b>. This violates the
>app-thread boundary and can make a chat turn fail before it emits its result; the same pattern also
>affects <b><i>_run_connect</i></b>.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/tui/app.py[R926-929]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-84e82b5444b5506723dc7fc5cd88d9ba0fac69e58acde86eeda61edd665f37eaR926-R929)</code>
>
>```diff
>+    @work(thread=True, exit_on_error=False)
>+    def _run_chat_turn(self, prompt: str) -> None:
>+        log = self.query_one("#activity", RichLog)
>+        session = self._chat_session()
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>The decorator explicitly starts a thread, and the worker then immediately invokes <b><i>query_one</i></b>; only
>subsequent log updates use the thread-marshalling API. The connect worker follows the same unsafe
>ordering.
></pre>
>
> <code>[ratchet/tui/app.py[876-894]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/tui/app.py/#L876-L894)</code>
> <code>[ratchet/tui/app.py[916-941]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/tui/app.py/#L916-L941)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>Threaded TUI workers access Textual widgets directly. Widget lookup and app-state interactions must occur on the UI thread, with worker results sent back through `call_from_thread`.
>
>## Issue Context
>The chat worker already uses `call_from_thread` for its log writes, but its initial widget/session access bypasses that mechanism; the connection worker has the analogous issue.
>
>## Fix Focus Areas
>- ratchet/tui/app.py[876-894]
>- ratchet/tui/app.py[916-941]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  5.  <s>Diffs bypass machinery guard</s> <code>✓ Resolved</code> <code>🐞 Bug</code> <code>⛨ Security</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>Chat validates <b><i>file:</i></b> fence paths against <b><i>.git</i></b> and <b><i>.ratchet</i></b>, but sends <b><i>diff</i></b> fences directly
>to <b><i>git apply</i></b> without the same check. A model can therefore create or modify <b><i>.ratchet</i></b> bus,
>receipt, or approval files, and those changes persist because the commit intentionally excludes
><b><i>.ratchet</i></b>.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/chat.py[R122-124]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-5282c92504ec5ccf7f520f6dee7f6227144b4fe63e9fe2dff845ef30d79d4971R122-R124)</code>
>
>```diff
>+        if diff and not self.cancel.is_set():
>+            applied = self._apply(diff.group(1))
>+            emit("step" if applied else "note", "applied diff" if applied else "diff did not apply; skipped")
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>Full-file writes explicitly reject <b><i>.git</i></b> and <b><i>.ratchet</i></b>, whereas <b><i>_apply</i></b> invokes <b><i>git apply</i></b> on
>the raw model diff; <b><i>_commit</i></b> then excludes <b><i>.ratchet</i></b>, leaving any such modification outside the
>revertible chat commit.
></pre>
>
> <code>[ratchet/chat.py[103-126]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/chat.py/#L103-L126)</code>
> <code>[ratchet/chat.py[138-149]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/chat.py/#L138-L149)</code>
> <code>[ratchet/chat.py[159-170]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/chat.py/#L159-L170)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>Model-generated diff fences bypass the protected-path validation applied to full-file fences.
>
>## Issue Context
>Parse and reject every diff touching `.git`, `.ratchet`, or paths outside the repository before invoking `git apply`.
>
>## Fix Focus Areas
>- ratchet/chat.py[103-126]
>- ratchet/chat.py[138-149]
>- ratchet/chat.py[159-170]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  6.  Dashboard executes event HTML <code>🐞 Bug</code> <code>⛨ Security</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
><b><i>drawDetail</i></b> assigns <b><i>summarize(a)</i></b> to <b><i>innerHTML</i></b>, while <b><i>summarize</i></b> interpolates model-controlled
>candidate <b><i>intent</i></b> and verifier <b><i>reason</i></b> values persisted on the JSONL bus. A model response
>containing HTML or JavaScript can therefore execute when a user opens the dashboard for that run, in
>the loopback dashboard origin that also has access to the approval endpoint.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/dashboard/index.html[788]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-9a0d7036770bc2e3936727b8ed6f77bd45f0524bdb22a14ca294e173f13013d3R788-R788)</code>
>
>```diff
>+  $("d-summary").innerHTML = summarize(a);
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>The loop emits the model candidate&#x27;s <b><i>intent</i></b> in <b><i>verify.started</i></b>, and the dashboard server replays
>the event stream unchanged to clients, where the value is stored as verifier state. <b><i>summarize</i></b>
>inserts <b><i>a.intent</i></b> and <b><i>a.reason</i></b> directly into an HTML string, and <b><i>drawDetail</i></b> parses that
>generated string by assigning it through <b><i>innerHTML</i></b>.
></pre>
>
> <code>[ratchet/loop.py[230-230]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/loop.py/#L230-L230)</code>
> <code>[ratchet/dashboard/index.html[700-721]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/index.html/#L700-L721)</code>
> <code>[ratchet/dashboard/index.html[777-805]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/index.html/#L777-L805)</code>
> <code>[ratchet/dashboard/index.html[710-731]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/index.html/#L710-L731)</code>
> <code>[ratchet/dashboard/index.html[777-804]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/index.html/#L777-L804)</code>
> <code>[ratchet/loop.py[221-235]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/loop.py/#L221-L235)</code>
> <code>[ratchet/dashboard/server.py[119-142]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/server.py/#L119-L142)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>Dashboard event text is interpolated into a string assigned via `innerHTML`, allowing model-controlled bus payloads such as candidate intent and verifier reason to execute as stored HTML or JavaScript.
>
>## Issue Context
>The JSONL event stream contains model-controlled content and is replayed to dashboard viewers. Keep static formatting separate from dynamic values, and insert every event-derived value through `textContent` or DOM text nodes rather than parsing it as HTML.
>
>## Fix Focus Areas
>- ratchet/dashboard/index.html[700-735]
>- ratchet/dashboard/index.html[777-805]
>- ratchet/loop.py[230-230]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  7.  Reject negative body lengths <code>🐞 Bug</code> <code>☼ Reliability</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>The approval handler only rejects lengths above <b><i>MAX_BODY</i></b>; a negative <b><i>Content-Length</i></b> reaches
><b><i>self.rfile.read(length)</i></b>, which reads until EOF rather than enforcing the configured limit. A
>client can keep such HTTP/1.1 connections open while streaming data, consuming unbounded threaded
>request handlers.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/dashboard/server.py[R152-158]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-f63da20b918dc78777dec901d93771d4a44623f0f759ac082e57f92a36db41e5R152-R158)</code>
>
>```diff
>+            length = int(self.headers.get("Content-Length") or 0)
>+        except ValueError:
>+            return self._send(400, b'{"error":"bad length"}')
>+        if length > MAX_BODY:
>+            return self._send(413, b'{"error":"too large"}')
>+        try:
>+            body = json.loads(self.rfile.read(length) or b"{}")
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>The handler parses the header as an integer and tests only its upper bound before passing it
>directly to <b><i>read</i></b>. The server is explicitly threaded to support long-lived SSE requests, making
>retained request connections consume separate daemon threads.
></pre>
>
> <code>[ratchet/dashboard/server.py[148-158]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/server.py/#L148-L158)</code>
> <code>[ratchet/dashboard/server.py[177-191]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/server.py/#L177-L191)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>`POST /approve` accepts negative `Content-Length` values. Passing that value to the buffered request reader bypasses `MAX_BODY` and waits for EOF.
>
>## Issue Context
>The dashboard uses `ThreadingHTTPServer`, so connections held open this way accumulate request threads.
>
>## Fix Focus Areas
>- ratchet/dashboard/server.py[151-158]
>- ratchet/dashboard/server.py[177-182]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  8.  Approval booleans are coerced <code>🐞 Bug</code> <code>≡ Correctness</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>The approval handler accepts any JSON shape and converts <b><i>allow</i></b> with <b><i>bool()</i></b>, so
><b><i>{&quot;allow&quot;:&quot;false&quot;}</i></b> is written as an approval while a valid non-object JSON body crashes the request
>handler at <b><i>.get</i></b>. This endpoint controls the gate decision file and must require an object with a
>literal JSON boolean.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/dashboard/server.py[R162-166]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-f63da20b918dc78777dec901d93771d4a44623f0f759ac082e57f92a36db41e5R162-R166)</code>
>
>```diff
>+        request_id = str(body.get("id", ""))
>+        if not SAFE_ID.match(request_id):
>+            return self._send(400, b'{"error":"bad id"}')
>+
>+        allow = bool(body.get("allow"))
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>The handler calls <b><i>.get</i></b> without checking the decoded type and uses <b><i>bool(body.get(&quot;allow&quot;))</i></b>;
><b><i>Gate.wait</i></b> then trusts the resulting file and immediately resolves the pending irreversible action.
></pre>
>
> <code>[ratchet/dashboard/server.py[151-173]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/server.py/#L151-L173)</code>
> <code>[ratchet/gate.py[70-83]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/gate.py/#L70-L83)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>The approval endpoint coerces truthy values to approval and assumes every JSON body is an object.
>
>## Issue Context
>Reject non-object bodies and require `allow` to be exactly a JSON boolean before writing a decision file.
>
>## Fix Focus Areas
>- ratchet/dashboard/server.py[151-173]
>- ratchet/gate.py[70-83]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


</details>
<br/>

<img src="https://img.shields.io/badge/Medium-634FD1?style=flat-square" height="20px" alt="Remediation recommended">

<details>
<summary>  9.  <b><i>do_POST</i></b> uses raw dictionary <code>📘 Rule violation</code> <code>⌂ Architecture</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>The new public HTTP handler accepts the fixed approval payload as a bare JSON dictionary with
>related <b><i>id</i></b> and <b><i>allow</i></b> fields. A dataclass boundary is required for this structured API input.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/dashboard/server.py[158]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-f63da20b918dc78777dec901d93771d4a44623f0f759ac082e57f92a36db41e5R158-R158)</code>
>
>```diff
>+            body = json.loads(self.rfile.read(length) or b"{}")
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>Rule 2986402 explicitly includes framework-visible handlers and requires fixed structured inputs
>with two or more related fields to use dataclasses. <b><i>do_POST</i></b> decodes a dictionary and then
>separately reads its <b><i>id</i></b> and <b><i>allow</i></b> fields.
></pre>
>
> <code>Rule 2986402: [Use dataclasses at public module and API boundaries](https://app.qodo.ai/rules/2986402?state=active)</code>
> <code>[ratchet/dashboard/server.py[148-173]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/server.py/#L148-L173)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>The dashboard approval API passes a fixed multi-field request payload through the handler as an untyped dictionary.
>
>## Issue Context
>Define a request dataclass for `id` and `allow`, validate JSON into it at the HTTP boundary, and use the typed instance in the handler.
>
>## Fix Focus Areas
>- ratchet/dashboard/server.py[148-173]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  10.  Provider failures escape handling <code>🐞 Bug</code> <code>☼ Reliability</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
><b><i>validate_key</i></b> and <b><i>ChatBackend.complete</i></b> let ordinary HTTP transport and response-decoding
>exceptions escape, while the chat and connect paths catch only <b><i>ChatProviderError</i></b>. Timeouts, DNS
>failures, or malformed provider responses therefore terminate the background worker without
>producing the intended user-visible failure result.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/providers.py[R101-105]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-98338f27b53ead38fe631340c1898d052529e932db8e9f8e1af00a5b29aca1a3R101-R105)</code>
>
>```diff
>+    if provider == "anthropic":
>+        r = httpx.get("https://api.anthropic.com/v1/models",
>+                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=timeout)
>+    else:
>+        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>Both provider call paths invoke <b><i>httpx</i></b> and parse JSON directly, but their callers only convert
><b><i>ChatProviderError</i></b> into activity-pane errors and run with <b><i>exit_on_error=False</i></b>.
></pre>
>
> <code>[ratchet/providers.py[95-108]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/providers.py/#L95-L108)</code>
> <code>[ratchet/providers.py[153-189]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/providers.py/#L153-L189)</code>
> <code>[ratchet/tui/app.py[876-887]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/tui/app.py/#L876-L887)</code>
> <code>[ratchet/tui/app.py[926-941]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/tui/app.py/#L926-L941)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>Common HTTP and response parsing failures bypass the provider error type handled by the TUI.
>
>## Issue Context
>Wrap transport, timeout, JSON-decoding, and unexpected response-shape failures as `ChatProviderError`, preserving useful context without exposing keys.
>
>## Fix Focus Areas
>- ratchet/providers.py[95-108]
>- ratchet/providers.py[153-189]
>- ratchet/tui/app.py[876-887]
>- ratchet/tui/app.py[926-941]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  11.  Graph target needs hidden setup <code>🐞 Bug</code> <code>⚙ Maintainability</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>The new <b><i>run-graph</i></b> Make target reads <b><i>demo-repo/patches/scripted_graph.json</i></b> but neither depends on
><b><i>demo</i></b> nor creates that fixture. On a fresh checkout, <b><i>make run-graph</i></b> fails immediately with
><b><i>FileNotFoundError</i></b> instead of running the advertised offline graph demo.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[Makefile[R40-41]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-76ed074a9305c04054cdebb9e9aad2d818052b07091de1f20cad0bbac34ffb52R40-R41)</code>
>
>```diff
>+run-graph:
>+	python -m ratchet.cli graph --file objectives/demo-graph.yaml --repo demo-repo --scripted demo-repo/patches/scripted_graph.json
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>The Make target passes a file under <b><i>demo-repo</i></b>, <b><i>cmd_graph</i></b> reads it unconditionally, and that file
>is only created as a side effect of the separate demo setup flow.
></pre>
>
> <code>[Makefile[34-41]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/Makefile/#L34-L41)</code>
> <code>[ratchet/cli.py[169-172]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/cli.py/#L169-L172)</code>
> <code>[ratchet/demo.py[207-212]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/demo.py/#L207-L212)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>`make run-graph` requires demo-generated files but does not declare or create that prerequisite.
>
>## Issue Context
>Make the target depend on the fixture-producing target or generate its scripted input independently.
>
>## Fix Focus Areas
>- Makefile[34-41]
>- ratchet/cli.py[169-172]
>- ratchet/demo.py[207-212]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details><summary><ins><strong>View medium (3)</strong></ins></summary><br/>
<details>
<summary>  12.  <b><i>dashboard/__init__.py</i></b> lacks future import <code>📘 Rule violation</code> <code>⚙ Maintainability</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>The new Python module imports <b><i>serve</i></b> without first enabling postponed annotation evaluation. This
>violates the requirement that every project Python module include `from __future__ import
>annotations` immediately after its docstring.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/dashboard/__init__.py[3]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-e5afc7dbb9a8554ce65bf5d6e66b2974900314c0fec19396009f4618d05c1bf0R3-R3)</code>
>
>```diff
>+from .server import serve
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>Rule 2986401 requires every Python source module to enable postponed annotation evaluation
>immediately after its docstring. The new module proceeds directly from its docstring to a regular
>import at line 3.
></pre>
>
> <code>Rule 2986401: [Always enable postponed evaluation of annotations in Python modules](https://app.qodo.ai/rules/2986401?state=active)</code>
> <code>[ratchet/dashboard/__init__.py[1-3]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/__init__.py/#L1-L3)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>The new dashboard package module lacks the mandatory `from __future__ import annotations` statement.
>
>## Issue Context
>Place the future import immediately after the module docstring and before the `serve` import.
>
>## Fix Focus Areas
>- ratchet/dashboard/__init__.py[1-3]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  13.  SSE reconnect loses sequence <code>📘 Rule violation</code> <code>≡ Correctness</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>The browser creates <b><i>EventSource</i></b> with a fixed <b><i>/events</i></b> URL and tracks no last sequence number, so
>automatic reconnections reuse that URL without <b><i>after_sequence_number</i></b>. Reconnection therefore
>cannot resume after the last processed event as required.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/dashboard/index.html[R1082-1085]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-9a0d7036770bc2e3936727b8ed6f77bd45f0524bdb22a14ca294e173f13013d3R1082-R1085)</code>
>
>```diff
>+const source = new EventSource("/events");
>+source.onmessage = (e) => {
>+  const { kind, payload, ts } = JSON.parse(e.data);
>+  apply(kind, payload || {}, ts);
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>Rule 2986435 requires every SSE reconnection to include <b><i>after_sequence_number</i></b> from client state.
>The only <b><i>EventSource</i></b> is constructed from the constant <b><i>/events</i></b>, and its message handler records
>only <b><i>kind</i></b>, <b><i>payload</i></b>, and <b><i>ts</i></b>, leaving no sequence value available for reconnection.
></pre>
>
> <code>Rule 2986435: [SSE reconnection must include after_sequence_number query parameter](https://app.qodo.ai/rules/2986435?state=active)</code>
> <code>[ratchet/dashboard/index.html[1082-1093]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/index.html/#L1082-L1093)</code>
> <code>[ratchet/dashboard/server.py[102-109]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/server.py/#L102-L109)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>The dashboard's SSE connection automatically reconnects to `/events` without an `after_sequence_number` derived from the last processed frame.
>
>## Issue Context
>Track each processed event's sequence number and implement reconnection with a URL containing `after_sequence_number`; update the server endpoint to honor that parameter.
>
>## Fix Focus Areas
>- ratchet/dashboard/index.html[1082-1093]
>- ratchet/dashboard/server.py[102-144]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


<details>
<summary>  14.  SSE dispatch uses <b><i>kind</i></b> <code>📘 Rule violation</code> <code>≡ Correctness</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>The dashboard parses SSE data but dispatches through the noncanonical <b><i>kind</i></b> field instead of the
>required JSON payload <b><i>type</i></b> field. Producers and consumers using the required schema will not route
>these events correctly.
></pre>
></details>

> <details>
><summary>Code</summary>
><br/>
>
><code>[ratchet/dashboard/index.html[R1084-1085]](https://github.com/ayaangazali/ratchet/pull/14/files#diff-9a0d7036770bc2e3936727b8ed6f77bd45f0524bdb22a14ca294e173f13013d3R1084-R1085)</code>
>
>```diff
>+  const { kind, payload, ts } = JSON.parse(e.data);
>+  apply(kind, payload || {}, ts);
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>Rule 2986434 requires SSE consumers to select handlers from the parsed JSON <b><i>type</i></b> property. The
>client destructures <b><i>kind</i></b> and passes it to <b><i>apply</i></b>, while the server emits <b><i>kind</i></b>, so the new SSE
>path never inspects a JSON <b><i>type</i></b> field.
></pre>
>
> <code>Rule 2986434: [Route SSE frames using JSON payload &quot;type&quot; field, not SSE event name](https://app.qodo.ai/rules/2986434?state=active)</code>
> <code>[ratchet/dashboard/index.html[1082-1085]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/index.html/#L1082-L1085)</code>
> <code>[ratchet/dashboard/server.py[129-131]](https://github.com/ayaangazali/ratchet/blob/190cbb8e5f4c39a682035f0bc0730ba0ad352e37/ratchet/dashboard/server.py/#L129-L131)</code>
></details>

> <details>
><summary>Agent prompt</summary>
><br/>
>
>```
>The issue below was found during a code review. Follow the provided context and guidance below and implement a solution
>
>## Issue description
>Dashboard SSE routing uses `kind` rather than the required JSON payload `type` field.
>
>## Issue Context
>Update both the server frame schema and browser consumer so logical event routing is keyed by `type`; do not route using an SSE event name.
>
>## Fix Focus Areas
>- ratchet/dashboard/index.html[1082-1085]
>- ratchet/dashboard/server.py[129-131]
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>


</details>

<img src="https://www.qodo.ai/wp-content/uploads/2025/11/light-grey-line.svg" height="10%" alt="Grey Divider">


<!-- qodo-context:start -->
<details><summary><strong>Context sources</strong></summary>

<div>&#x2705; Compliance rules (platform): <a href="https://app.qodo.ai/rules?state=active&amp;scopes=/ayaangazali/ratchet/"><code>37 rules</code></a></div>
<div>&#x2705; Web pages:</div>
<div>&nbsp;&nbsp;<a href="https://textual.textualize.io/guide/workers/"><code>🌐 Workers - Textual</code></a></div>
<div>&nbsp;&nbsp;<a href="https://github.com/Textualize/textual/issues/4889"><code>🌐 Worker on cancelled don&#x27;t stop running</code></a></div>
<div>&nbsp;&nbsp;<a href="https://textual.textualize.io/api/work/"><code>🌐 textual.work - Textual</code></a></div>
<div>&nbsp;&nbsp;<code>+2 more</code></div>
<div>Review mode: <code>🧠 Deep</code>: This is a dense, cross-cutting change spanning TUI, dashboard/server approval handling, CLI packaging, core execution/verifier paths, and many independent logic sites, making redundant review materially useful.</div>
<!-- qodo-context:end -->
</details>

<img src="https://www.qodo.ai/wp-content/uploads/2025/11/light-grey-line.svg" height="10%" alt="Grey Divider">



<!-- qodo-daily-tip:start -->

<details>
<summary><strong>Tip of the day</strong></summary>

<br/>

<pre>💡 Did you know, you can enable the Remediation agent and Qodo fixes findings in a dedicated fix PR</pre>

<a href="https://docs.qodo.ai/tips-and-tricks">More tips ↗</a> | <a href="https://app.qodo.ai/configurations?tab=display-preferences">Customize Qodo ↗</a> | <a href="https://docs.qodo.ai">Qodo docs ↗</a>

</details>

<img src="https://www.qodo.ai/wp-content/uploads/2025/11/light-grey-line.svg" height="10%" alt="Grey Divider">
<!-- qodo-daily-tip:end -->


<!-- https://github.com/ayaangazali/ratchet/commit/190cbb8e5f4c39a682035f0bc0730ba0ad352e37 -->

<a href="https://www.qodo.ai"><img src="https://www.qodo.ai/wp-content/uploads/2025/03/qodo-logo.svg" width="80" alt="Qodo Logo"></a>
