from __future__ import print_function

import argparse
import atexit
import datetime
import distutils
import gettext
import itertools
import json
import os
import pexpect
import pipes
import re
import readline
import requests
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termcolor
import tempfile
import textwrap
import time
import traceback

from backports.shutil_get_terminal_size import get_terminal_size
from backports.shutil_which import which
from distutils.spawn import find_executable
from distutils.version import StrictVersion
from pkg_resources import get_distribution, parse_version
from six.moves import urllib
from threading import Thread

# Internationalization
gettext.bindtextdomain("messages", os.path.join(sys.path[0], "locale"))
gettext.textdomain("messages")
_ = gettext.gettext

# globals
# require python 2.7+
if sys.version_info < (2, 7):
    sys.exit(_("You have an old version of python. Install version 2.7 or higher."))
if sys.version_info < (3, 0):
    input = raw_input
if not hasattr(shlex, "quote"):
    shlex.quote = pipes.quote

ORG = "submit50"
timestamp = None


class Error(Exception):
    """Exception raised for errors."""
    pass


class _Getch:
    """
    Get a single character from standard input.

    https://stackoverflow.com/a/510364
    """

    class _GetchUnix:
        def __init__(self):
            import tty, sys

        def __call__(self):
            import sys, termios, tty
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            return ch

    class _GetchWindows:
        def __init__(self):
            import msvcrt

        def __call__(self):
            import msvcrt
            return msvcrt.getch()

    def __init__(self):
        try:
            self.impl = _Getch._GetchWindows()
        except ImportError:
            self.impl = _Getch._GetchUnix()

    def __call__(self):
        return self.impl()


getch = _Getch()


# submit50
def main():

    # listen for ctrl-c
    signal.signal(signal.SIGINT, handler)

    # clean up on normal exit
    atexit.register(teardown)

    # define command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose",
                        action="store_true",
                        help=_("show commands being executed"))
    parser.add_argument("problem", help=_("problem to submit"))
    args = vars(parser.parse_args())

    # submit50 -v
    # submit50 --verbose
    if args["verbose"]:
        run.verbose = True

    # submit50 problem
    submit("submit50", args["problem"])

    # kthxbai
    sys.exit(0)


def authenticate(org):
    """Authenticate user."""

    # cache credentials in ~/.git-credential-cache/:org
    cache = os.path.expanduser("~/.git-credential-cache")
    try:
        os.mkdir(cache, 0o700)
    except:
        pass
    authenticate.SOCKET = os.path.join(cache, ORG)

    spawn = pexpect.spawn if sys.version_info < (3, 0) else pexpect.spawnu
    child = spawn("git -c credential.helper='cache --socket {}' credential fill".format(authenticate.SOCKET))
    child.sendline("")

    if child.expect(["Username:", pexpect.EOF]):
        # Credentials are already cached
        clear_credentials()
        username, password = re.search("username=([^\r]+)\r\npassword=([^\r]+)", child.before, re.MULTILINE).groups()
    else:
        # No cached credentials found
        try:
            username = run("git config --global credential.https://github.com/submit50.username")
        except Error:
            username = None
        password = None
    child.close()

    def rlinput(prompt, prefill=""):
        """
        Input function that uses a prefill value and advanced line editing.

        https://stackoverflow.com/a/2533142
        """
        readline.set_startup_hook(lambda: readline.insert_text(prefill))
        try:
            return input(prompt)
        finally:
           readline.set_startup_hook()

    # prompt for credentials
    progress(False) # because not using cprint herein
    if not password:

        # prompt for username, prefilling if possible
        while True:
            progress(False)
            try:
                username = rlinput(_("GitHub username: "), username).strip()
                if username:
                    break
            except EOFError:
                print()

        # prompt for password
        while True:
            print(_("GitHub password: "), end="")
            sys.stdout.flush()
            password = str()
            while True:
                ch = getch()
                if ch in ["\n", "\r"]: # Enter
                    print()
                    break
                elif ch == "\177": # DEL
                    if len(password) > 0:
                        password = password[:-1]
                        print("\b \b", end="")
                        sys.stdout.flush()
                elif ch == "\3": # ctrl-c
                    print("^C", end="")
                    os.kill(os.getpid(), signal.SIGINT)
                elif ch == "\4": # ctrl-d
                    print()
                    break
                else:
                    password += ch
                    print("*", end="")
                    sys.stdout.flush()
            if password:
                break

    # authenticate user
    email = "{}@users.noreply.github.com".format(username)
    res = requests.get("https://api.github.com/user",
                       auth=(username, password))

    # check for 2-factor authentication
    # http://github3.readthedocs.io/en/develop/examples/oauth.html?highlight=token
    if "X-GitHub-OTP" in res.headers:
        password = two_factor(org, username, password)
        res = requests.get("https://api.github.com/user",
                           auth=(username, password))

    # check if incorrect password
    if res.status_code == 401:
        raise Error(_("Invalid username and/or password."))

    # check for other error
    elif res.status_code != 200:
        raise Error(_("Could not authenticate user."))

    # canonicalize (capitalization of) username,
    # especially if user logged in via email address
    username = res.json()["login"]

    # cache credentials for 1 week
    timeout = int(datetime.timedelta(weeks=1).total_seconds())
    run("git -c credential.helper='cache --socket {} --timeout {}' "
        "-c credentialcache.ignoresighup=true credential approve".format(authenticate.SOCKET, timeout),
        lines=["username={}".format(username), "password={}".format(password), "", ""],
        quiet=True)

    # return credentials
    return (username, password, email)


authenticate.SOCKET = None


def clear_credentials():
    """Clear git credential cache """
    run("git credential-cache --socket {} exit".format(authenticate.SOCKET))
    # OSX will sometimes store git credentials in the keyring. Try to remove them
    try:
        run("git credential-osxkeychain erase", lines=["host=github.com", "protocol=https", ""])
    except Error:
        pass


def cprint(text="", color=None, on_color=None, attrs=None, **kwargs):
    """Colorizes text (and wraps to terminal's width)."""

    # update progress
    progress(False)

    # assume 80 in case not running in a terminal
    columns, lines = get_terminal_size()
    if columns == 0: columns = 80 # because get_terminal_size's default fallback doesn't work in pipes

    # print text
    termcolor.cprint(textwrap.fill(text, columns, drop_whitespace=False),
                     color=color, on_color=on_color, attrs=attrs, **kwargs)


def excepthook(type, value, tb):
    """Report an exception."""
    excepthook.ignore = False
    progress(False)
    teardown()
    if type is Error and str(value):
        cprint(str(value), "yellow")
    elif type is requests.exceptions.ConnectionError:
        cprint(_("Could not connect to GitHub."), "yellow")
    else:
        if run.verbose:
            traceback.print_exception(type, value, tb)
        cprint(_("Sorry, something's wrong! Let sysadmins@cs50.harvard.edu know!"), "yellow")
    if authenticate.SOCKET: # not set when using SSH
        try:
            clear_credentials()
        except Exception:
            pass
        cprint(_("Submission cancelled."), "red")

sys.excepthook = excepthook


def handler(number, frame):
    """Handle SIGINT."""
    os.system("stty sane 2> {}".format(os.devnull)) # in case signalled from input_with_prefill
    if progress.progressing:
        progress(False)
    else:
        cprint()
    try:
        clear_credentials()
    except Exception:
        pass
    teardown()
    cprint(_("Submission cancelled."), "red")
    os._exit(0)


def run(command, cwd=None, env=None, lines=[], password=None, quiet=False, timeout=None):
    """Run a command."""

    # echo command
    if run.verbose:
        cprint(command, attrs=["bold"])

    # include GIT_DIR and GIT_WORK_TREE in env
    if not env:
        env = {
            "GIT_DIR": run.GIT_DIR,
            "GIT_WORK_TREE": run.GIT_WORK_TREE,
            "HOME": os.path.expanduser("~")
        }
        if os.getenv("SSH_AGENT_PID"):
            env["SSH_AGENT_PID"] = os.getenv("SSH_AGENT_PID")
        if os.getenv("SSH_AUTH_SOCK"):
            env["SSH_AUTH_SOCK"] = os.getenv("SSH_AUTH_SOCK")

    # spawn command
    if sys.version_info < (3, 0):
        child = pexpect.spawn(command, cwd=cwd, env=env, ignore_sighup=True, timeout=timeout)
    else:
        child = pexpect.spawnu(command, cwd=cwd, encoding="utf-8", env=env, ignore_sighup=True, timeout=timeout)

    # send output of command to stdout only if run with --verbose (and not quieted by caller)
    if run.verbose and not quiet:
        child.logfile_read = sys.stdout

    # wait for prompt, send password
    if password:
        res = child.expect(["Password for '.*': ", pexpect.EOF])
        if res == 0:
            child.sendline(password)

    # send lines of input
    for line in lines:
        child.sendline(line)

    # read output, check status
    command_output = child.read().strip()
    child.close()
    if child.signalstatus is None and child.exitstatus != 0:
        raise Error()
    return command_output


run.GIT_DIR = tempfile.mkdtemp()
run.GIT_WORK_TREE = os.getcwd()
run.verbose = False


def progress(message=""):
    """Display a progress bar as dots."""

    # don't show in verbose mode
    if run.verbose:
        if message != False:
            print(message + "...")
        return

    # stop progressing if already progressing
    if progress.progressing:
        progress.progressing = False
        progress.thread.join()
        sys.stdout.write("\n")
        sys.stdout.flush()

    # display dots if message passed
    if message != False:
        def progress_helper():
            sys.stdout.write(message + "...")
            sys.stdout.flush()
            while progress.progressing:
                sys.stdout.write(".")
                sys.stdout.flush()
                time.sleep(0.5)
        progress.progressing = True
        progress.thread = Thread(target=progress_helper)
        progress.thread.start()


progress.progressing = False


def submit(org, branch):
    """Submit problem."""

    # check announcements
    res = requests.get("https://cs50.me/status/submit50")
    if res.status_code == 200 and res.text.strip():
        raise Error(res.text.strip())

    # require git 2.7+, so that credential-cache--daemon ignores SIGHUP
    # https://github.com/git/git/blob/v2.7.0/credential-cache--daemon.c
    if not which("git"):
        raise Error(_("You don't have git. Install git, then re-run {}!".format(org)))
    version = subprocess.check_output(["git", "--version"]).decode("utf-8")
    matches = re.search(r"^git version (\d+\.\d+\.\d+).*$", version)
    if not matches or StrictVersion(matches.group(1)) < StrictVersion("2.7.0"):
        raise Error(_("You have an old version of git. Install version 2.7 or later, then re-run {}!".format(org)))

    # update progress
    progress("Connecting")

    # compute timestamp
    global timestamp
    headers = requests.get("https://api.github.com/").headers
    timestamp = datetime.datetime.strptime(headers["Date"], "%a, %d %b %Y %H:%M:%S %Z")
    timestamp = timestamp.strftime("%Y%m%dT%H%M%SZ")

    # check version
    res = requests.get("https://cs50.me/versions/submit50")
    if res.status_code != 200:
        raise Error(_("You have an unknown version of submit50. "
                      "Email sysadmins@cs50.harvard.edu!"))
    version_required = res.text.strip()
    if parse_version(version_required) > parse_version(get_distribution("submit50").version):
        raise Error(_("You have an old version of submit50. "
                      "Run update50, then re-run {}!".format(org)))

    file, submit.EXCLUDE = tempfile.mkstemp()

    # separate branch into problem slug and source repo
    check_repo = "@cs50/checks"
    branch = branch if not branch.endswith(check_repo) else branch[:-len(check_repo)]
    try:
        slug, src = branch.split("@")
    except ValueError:
        slug, src = branch, "cs50/checks"

    # ensure problem exists
    url = "https://raw.githubusercontent.com/{}/master/{}/submit50/exclude".format(src, slug)
    try:
        urllib.request.urlretrieve(url, filename=submit.EXCLUDE)
        lines = open(submit.EXCLUDE)
    except Exception as e:
        if run.verbose:
            cprint(str(e))
        e = Error(_("Invalid problem. Did you mean to submit something else?"))
        e.__cause__ = None
        raise e

    # check for missing files
    missing = []
    for line in lines:
        matches = re.match(r"^\s*#\s*([^\s]+)\s*$", line)
        if matches:
            pattern = matches.group(1)
            if pattern[:-1] == "/":
                if not os.path.isdir(pattern):
                    missing.append(pattern)
            elif not os.path.isfile(pattern):
                missing.append(pattern)
    if missing:
        cprint(_("You seem to be missing these files:"))
        for pattern in missing:
            cprint(" {}".format(pattern))
        raise Error(_("Ensure you have the required files before submitting."))

    # update progress
    progress(_("Authenticating"))

    # authenticate user via SSH
    try:

        # require ssh
        assert which("ssh")

        # require GitHub username in ~/.gitconfig
        username, password = run("git config --global credential.https://github.com/submit50.username", quiet=True), None
        email = "{}@users.noreply.github.com".format(username)
        repo = "git@github.com:{}/{}.git".format(org, username)
        progress(False)

        # require ssh-agent
        child = pexpect.spawn("ssh git@github.com")
        i = child.expect(["Enter passphrase for key", pexpect.EOF])
        child.close()
        assert i != 0

    # authenticate user via HTTPS
    except:
        username, password, email = authenticate(org)
        repo = "https://{}@github.com/{}/{}".format(username, org, username)

    # update progress
    progress(_("Preparing"))

    # clone repository
    try:
        run("git clone --bare {} {}".format(shlex.quote(repo), shlex.quote(run.GIT_DIR)), password=password)
    except:
        if password:
            e = Error(_("Looks like {} isn't enabled for your account yet. "
                        "Log into https://cs50.me/ in a browser, click \"Authorize application\", and re-run {} here!".format(org, org)))
        else:
            e = Error(_("Looks like you have the wrong username in ~/.gitconfig or {} isn't yet enabled for your account. "
                        "Double-check ~/.gitconfig and then log into https://cs50.me/ in a browser, "
                        "click \"Authorize application\" if prompted, and re-run {} here.".format(org, org)))
        e.__cause__ = None
        raise e

    # check out .gitattributes, if any, temporarily shadowing student's, if any
    if os.path.isfile(".gitattributes"):
        submit.ATTRIBUTES = ".gitattributes.{}".format(round(time.time()))
        os.rename(".gitattributes", submit.ATTRIBUTES)
    try:
        run("git checkout --force {} .gitattributes".format(branch))
    except Exception:
        pass

    # set options
    tag = "{}@{}".format(branch, timestamp)
    run("git config user.email {}".format(shlex.quote(email)))
    run("git config user.name {}".format(shlex.quote(username)))
    run("git symbolic-ref HEAD refs/heads/{}".format(shlex.quote(branch)))

    # patterns of file names to exclude
    run("git config core.excludesFile {}".format(shlex.quote(submit.EXCLUDE)))

    # blocklist for git-lfs
    # https://github.com/git-lfs/git-lfs/blob/master/commands/command_track.go
    with open("{}/info/exclude".format(run.GIT_DIR), "w") as file:
        file.write(".git*\n")
        file.write(".lfs*\n")

    # adds, modifies, and removes index entries to match the working tree
    run("git add --all")

    # get file lists
    files = run("git ls-files").split()
    other = run("git ls-files --exclude-standard --other").split()

    # check for large files > 100 MB (and huge files > 2 GB)
    # https://help.github.com/articles/conditions-for-large-files/
    # https://help.github.com/articles/about-git-large-file-storage/
    large, huge = [], []
    for file in files:
        size = os.path.getsize(file)
        if size > (100 * 1024 * 1024):
            large.append(file)
        elif size > (2 * 1024 * 1024 * 1024):
            huge.append(file)
    if len(huge) > 0:
        raise Error(_("These files are too large to be submitted:\n{}\n"
                      "Remove these files from your directory "
                      "and then re-run {}!").format("\n".join(huge), org))
    elif len(large) > 0:
        if not which("git-lfs"):
            raise Error(_("These files are too large to be submitted:\n{}\n"
                          "Install git-lfs (or remove these files from your directory) "
                          "and then re-run {}!").format("\n".join(large), org))
        run("git lfs install --local")
        run("git config credential.helper cache") # for pre-push hook
        for file in large:
            run("git lfs track {}".format(file))
        run("git add --force .gitattributes")

    # files that will be submitted
    if len(files) == 0:
        raise Error(_("No files in this directory are expected for submission."))

    # prompts for submit50
    if org == "submit50":
        if len(files) == 1:
            cprint(_("Files that will be submitted:"), "green")
        for f in files:
            cprint("./{}".format(f), "green")

        # files that won't be submitted
        if len(other) != 0:
            cprint(_("Files that won't be submitted:"), "yellow")
            for f in other:
                cprint("./{}".format(f), "yellow")

        # prompt for honesty
        try:
            answer = input(_("Keeping in mind the course's policy on academic honesty, "
                             "are you sure you want to submit these files? "))
        except EOFError:
            answer = None
            print()
        if not answer or not re.match("^\s*(?:y|yes)\s*$", answer, re.I):
            raise Error(_("No files were submitted."))

    # update progress
    if org == "submit50":
        progress(_("Submitting"))
    else:
        progress(_("Uploading"))

    # push branch
    run("git commit --allow-empty --message='{}'".format(timestamp))
    commit_hash = run("git rev-parse HEAD")
    run("git push origin 'refs/heads/{}'".format(branch), password=password)

    # successful submission
    if org == "submit50":
        cprint(_("Submitted {}! See https://cs50.me/submissions.").format(branch),
               "green")
    progress(False)
    return username, commit_hash


submit.ATTRIBUTES = None
submit.EXCLUDE = None


def teardown():
    """Delete temporary directory and temporary file, restore any attributes."""
    if os.path.isfile(".gitattributes"):
        try:
            os.remove(".gitattributes")
        except Exception:
            pass
    if submit.ATTRIBUTES:
        try:
            os.rename(submit.ATTRIBUTES, ".gitattributes")
        except Exception:
            pass
    shutil.rmtree(run.GIT_DIR, ignore_errors=True)
    if submit.EXCLUDE:
        try:
            os.remove(submit.EXCLUDE)
        except Exception:
            pass


def two_factor(org, username, password):
    """Get one-time authentication code."""
    # send authentication request
    requests.post("https://api.github.com/authorizations",
                  auth=(username, password))
    while True:
        cprint("Authentication code:", end=" ")
        sys.stdout.flush()
        code = input()
        if code:
            break
    data = json.dumps({"scopes": ["repo", "user"], "note": "{} {}".format(org, timestamp)})
    res = requests.post("https://api.github.com/authorizations",
                        auth=(username, password),
                        data=data,
                        headers={"X-GitHub-OTP": str(code)})
    if res.status_code == 201 and "token" in res.json():
        return res.json()["token"]
    else:
        raise Error(_("Could not complete two-factor authentication."))


if __name__ == "__main__":
    main()
