# Synthetic sender/receiver joint native proof

`scripts/verify_sender_receiver_joint_native.py` runs a separate fresh native
Linux AMD64 rehearsal. It preserves the prior Miniflux ten-case fixture and its
held originals. It needs the compiled locked Miniflux binary/image and an
application image built from the same tested repository revision:

```sh
python scripts/verify_sender_receiver_joint_native.py \
  --repo "$GITHUB_WORKSPACE" --source "$RUNNER_TEMP/miniflux-pause" \
  --binary "$RUNNER_TEMP/pause-build/miniflux" \
  --sender-image "$sender_image_id" --receiver-image "$receiver_image_id" \
  --output "$RUNNER_TEMP/sender-receiver-joint" --run
```

Use the ordinary root `Dockerfile` for the receiver image, with
`--build-arg AI_FILTER_BUILD_GIT_SHA=$GITHUB_SHA`; the image ID and revision label
are checked. The fixture script is mounted read-only. No `.126` image/tag,
existing container, production configuration, production database or real model
is used. Host-only `requirements-ops.txt` provides psycopg. Dependencies must
already be available to the invoking workflow.

The new internal Docker network has no published ports. Each sender, receiver
web and receiver worker has 768 MiB RAM, no extra swap, 256 PIDs and one CPU;
PostgreSQL retains 384 MiB / 64 PIDs / one CPU. The receiver worker explicitly
holds its synthetic component identity; the fixture web process runs the real
queue claim/execute/complete path with a fake scoring function. It does not
claim real model or real business-database processing. The outer workflow's
20-minute limit and the existing harness's 600-second bound are unchanged.

The actual Go sender creates a durable singleton outbox body while the owned
receiver has not started. SenderDriver stops producers and seals exact raw
bytes through a real read-only PostgreSQL snapshot. The actual receiver then
starts, stops new intake, transfers those originals into the real queue and
ReceiptStore, and accepts only their exact authenticated duplicates. Go's normal
outbox POST receives the actual Flask durable receipt and records its ACK in
PostgreSQL. No ACK is injected with SQL. The fake scorer consumes the real task,
and SenderDriver independently observes no unfinished/held originals before
full pause. JointDrainController revalidates the original stop/pause intent
chain, raw bundle and exact completed transfer, and queries the actual current
sender pause and receiver stop. While the receiver task is still queued, a
freeze attempt must fail without writing a freeze intent or disabling drain.
After the real queue task completes, the controller holds release/refresh,
receiver controller/activity, spool/journal and both PostgreSQL protocol locks,
checks the confirmed pause operation, independently observes zero unfinished
storage, and only then writes the receiver freeze intent and receipt.

Success is one `CASE_REAL_GO_FLASK_DURABLE_DRAIN_AND_FROZEN_INVENTORY.json` plus
`RESULT.json` with `status=passed`. All commands, phase receipts, raw synthetic
originals, original SQL dump, resource/OOM observations and failures are retained.
Neither a successful transfer nor this synthetic case grants switch permission
or proves any historical uninstrumented sender originals.
