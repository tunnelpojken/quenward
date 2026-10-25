# Compose editor

[← Back to the README](../README.md)

Viewing and editing compose files, and giving QuenWard permission to save them.

Click a stack's compose file name to view and edit it. Saving:

1. **Refuses if the file changed on disk** since you opened it (an edit over SSH
   is never overwritten), and while an action runs on the stack.
2. **Checks the YAML**, then runs **`docker compose config`** on the new text in
   the stack's own folder, so relative paths and the stack's `.env` resolve
   exactly as they will on start. Compose's own error message is shown.
3. **Keeps the previous version as `compose.yaml.bak`** (one generation), then
   writes the file **in place**, so it keeps its owner and permissions.

Saving does not touch running containers: **Apply** (`docker compose up -d`)
recreates the services that changed. `.env` files are never shown or edited, and
the audit log records hashes and line counts, never the content (compose files
often hold passwords). The app's own stack is shown read-only, and so is any stack that would run under
the app's own compose project name (a copy of its compose file, or a `name:` set
to it): starting or stopping it would act on the app itself. A save that would
give a stack that name is refused. In compose's error messages, values from the
stack's `.env` are replaced by `[value from .env]`.

**Allowing the app to write.** The app runs as user 46337, and your compose files
belong to you. Give it write access to the stack folders and compose files only,
not to everything inside (stack data stays out of reach), with an ACL:

```bash
sudo apt install acl   # if setfacl is missing
cd /home/you/docker
for d in */; do
  sudo setfacl -m u:46337:rwx "$d"                 # to create the .bak and a check file
  for f in "$d"compose.yaml "$d"compose.yml "$d"docker-compose.yaml "$d"docker-compose.yml; do
    [ -f "$f" ] && sudo setfacl -m u:46337:rw "$f"
  done
done
```

Run it again after adding a stack. Without write access the editor shows the file
read-only. Some editors replace a file when saving (a new file without the ACL);
if a stack turns read-only after an SSH edit, run the loop again.

The stacks folder must be mounted without `:ro` for this.

Why 46337: user IDs are shared between the host and every container. Many images
run as 10001 or 1000, and a container with the same UID as this app could read
and write what the app can (and the other way around). Check with
`docker ps -q | xargs -n1 docker top | awk 'NR>1{print $1}' | sort -u` that no
container of yours runs as 46337. To use another UID, build with
`--build-arg APP_UID=...` and use that number in the commands above.
