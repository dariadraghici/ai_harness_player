"""Build the image for linux/amd64.

  python scripts/build.py <tag>        e.g. dev, t1, t2, t3

The image is labeled with the Git commit it was built from (plus "-dirty" when
the working tree has changes), so scripts/push.py can refuse to push an image
whose source is not committed.
"""

import sys

from _common import git_commit, git_dirty, image_ref, run


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/build.py <tag>")
    ref = image_ref(sys.argv[1])
    rev = git_commit() + ("-dirty" if git_dirty() else "")
    run(["docker", "build", "--platform", "linux/amd64", "--label",
         "org.opencontainers.image.revision=" + rev, "-t", ref, "."])
    print("built %s from %s" % (ref, rev))


if __name__ == "__main__":
    main()
