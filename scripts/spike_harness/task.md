You are agent-main, working in the current directory, which holds seven meridian-* repos. Do each step below in order with your tools, one tool call per step, then stop and reply with one line per step saying what happened.

1. Read meridian-serving/README.md and note its first line.
2. Create the file meridian-serving/SPIKE.md containing the line "spike: first line", then edit it so the line reads "spike: edited line".
3. Run the shell command: cat meridian-serving/SPIKE.md
4. Use the run-tests skill to run the tests of meridian-serving.
5. Call the open_pr tool with repo "meridian-serving" and title "spike: HARNESS_NAME".
6. Call the send tool with to "serving-agent" and body "hello from HARNESS_NAME".
7. Call the send tool with to "agent-main" and body "note to self from HARNESS_NAME".
8. Call the read tool.
9. Call the unread tool.
