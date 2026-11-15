
"""Push the image to Harbor (ask Sebi first; only Sebi pushes freezes).

  python scripts/push.py <tag>         e.g. t1, t2, t3

Refuses when the Git working tree is dirty or when the local image was not
built from the current HEAD (source must match the image). Logs in with
--password-stdin (the secret never appears on a command line), pushes, then
prints the registry digest and the Git commit, and reminds you to tag it.
"""

import json
import subprocess
import sys

from _common import REGISTRY, SECRETS, git_commit, git_dirty, image_ref, read_env_file, run


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/push.py <tag>")
    tag = sys.argv[1]
    ref = image_ref(tag)
    dirty = git_dirty()
    if dirty:
        sys.exit("refusing to push: the Git working tree is dirty (commit first):\n" + dirty)
    head = git_commit()
    r = subprocess.run(["docker", "image", "inspect", ref, "--format",
                        '{{index .Config.Labels "org.opencontainers.image.revision"}}'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("image %s not found locally; run: python scripts/build.py %s" % (ref, tag))
    if r.stdout.strip() != head:
        sys.exit("refusing to push: image was built from %r but HEAD is %s; rebuild it" % (
            r.stdout.strip(), head))

    s = read_env_file(SECRETS)
    user, secret = s.get("HARBOR_USER"), s.get("HARBOR_SECRET")
    if not user or not secret:
        sys.exit("HARBOR_USER / HARBOR_SECRET missing in secrets.local.env")
    login = subprocess.run(["docker", "login", s.get("HARBOR_REGISTRY") or REGISTRY,
                            "--username", user, "--password-stdin"],
                           input=secret + "\n", capture_output=True, text=True)
    if login.returncode != 0:
        sys.exit("docker login failed: " + login.stderr.strip())
    print("docker login ok")

    run(["docker", "push", ref])
    r = run(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"],
            capture=True)
    digest = "?"
    try:
        digest = json.loads(r.stdout).get("digest", "?")
    except Exception:
        print(r.stdout)
    print("")
    print("pushed   : %s" % ref)
    print("digest   : %s" % digest)
    print("commit   : %s" % head)
    print("")
    print("REMINDER: tag this commit and push the tag so the judges see the source:")
    print("  git tag -f %s %s && git push -f origin %s" % (tag, head, tag))


if __name__ == "__main__":
    main()
