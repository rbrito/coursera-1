#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
For downloading lecture resources such as videos for Coursera classes. Given
a class name, username and password, it scrapes the course listing page to
get the section (week) and lecture names, and then downloads the related
materials into appropriately named files and directories.

Examples:
  coursera-dl -u <user> -p <passwd> saas
  coursera-dl -u <user> -p <passwd> -l listing.html -o saas --skip-download

For further documentation and examples, visit the project's home at:
  https://github.com/jplehmann/coursera

Authors and copyright:
    © 2012-2013, John Lehmann (first last at geemail dotcom or @jplehmann)
    © 2012-2013, Rogério Brito (r lastname at ime usp br)

Contributions are welcome, but please add new unit tests to test your changes
and/or features.  Also, please try to make changes platform independent and
backward compatible.

Legalese:

 This program is free software: you can redistribute it and/or modify it
 under the terms of the GNU Lesser General Public License as published by
 the Free Software Foundation, either version 3 of the License, or (at your
 option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import cookielib
import datetime
import errno
import getpass
import logging
import netrc
import os
import platform
import re
import string
import StringIO
import subprocess
import sys
import tempfile
import time
import requests
import urllib
import urllib2

try:
    from BeautifulSoup import BeautifulSoup
except ImportError:
    from bs4 import BeautifulSoup

csrftoken = ''
session = ''
NEW_AUTH_URL = 'https://www.coursera.org/maestro/api/user/login'


class ClassNotFound(BaseException):
    """
    Class to be thrown if a course is not found in Coursera's site.
    """

    pass


class BandwidthCalc(object):
    """
    Class for calculation of bandwidth for the "native" downloader.
    """

    def __init__(self):
        self.nbytes = 0
        self.prev_time = time.time()
        self.prev_bw = 0
        self.prev_bw_length = 0

    def received(self, data_length):
        now = time.time()
        self.nbytes += data_length
        time_delta = now - self.prev_time

        if time_delta > 1:  # average over 1+ second
            bw = float(self.nbytes) / time_delta
            self.prev_bw = (self.prev_bw + 2 * bw) / 3
            self.nbytes = 0
            self.prev_time = now

    def __str__(self):
        if self.prev_bw == 0:
            bw = ''
        elif self.prev_bw < 1000:
            bw = ' (%dB/s)' % self.prev_bw
        elif self.prev_bw < 1000000:
            bw = ' (%.2fKB/s)' % (self.prev_bw / 1000)
        elif self.prev_bw < 1000000000:
            bw = ' (%.2fMB/s)' % (self.prev_bw / 1000000)
        else:
            bw = ' (%.2fGB/s)' % (self.prev_bw / 1000000000)

        length_diff = self.prev_bw_length - len(bw)
        self.prev_bw_length = len(bw)

        if length_diff > 0:
            return '%s%s' % (bw, length_diff * ' ')
        else:
            return bw


def get_syllabus_url(className):
    """
    Return the Coursera index/syllabus URL.
    """

    return 'https://class.coursera.org/%s/lecture/index' % className


def write_cookie_file(className, username, password):
    """
    Automatically generate a cookie file for the Coursera site.
    """
    global csrftoken
    global session

    s = requests.Session()
    r = s.get(get_syllabus_url(className))

    csrftoken = r.cookies['csrf_token']

    # The next data will be sent in a POST request.
    std_headers = {
        'Cookie': ('csrftoken=%s' % csrftoken),
        'Referer': 'https://www.coursera.org',
        'X-CSRFToken': csrftoken,
        }

    s.headers.update(std_headers)

    auth_data = {
        'email_address': username,
        'password': password
        }

    r = requests.post(NEW_AUTH_URL, data=auth_data)

    if r.status_code == 404:
        raise ClassNotFound(className)
    else:
        raise ClassNotFound('Error %d with %s' %(r.status_code, className))

    session = s


def down_the_wabbit_hole(className, cookies_file):
    """
    Get the session cookie
    """
    quoted_class_url = urllib.quote_plus(get_syllabus_url(className))
    auth_redirector_url = 'https://class.coursera.org/%s/auth/auth_redirector?type=login&subtype=normal&email=&visiting=%s' % (className, quoted_class_url)

    global session
    cj = get_cookie_jar(cookies_file)

    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj),
                                  urllib2.HTTPHandler(),
                                  urllib2.HTTPSHandler())

    req = urllib2.Request(auth_redirector_url)
    opener.open(req)

    for cookie in cj:
        if cookie.name == 'session':
            session = cookie.value
            break
    opener.close()


def get_config_paths(config_name, user_specified_path=None):
    """
    Returns a list of config files paths to try in order, given config file
    name and possibly a user-specified path
    """

    # For Windows platforms, there are several paths that can be tried to
    # retrieve the netrc file. There is, however, no "standard way" of doing
    # things.
    #
    # A brief recap of the situation (all file paths are written in Unix
    # convention):
    #
    # 1. By default, Windows does not define a $HOME path. However, some
    # people might define one manually, and many command-line tools imported
    # from Unix will search the $HOME environment variable first. This
    # includes MSYSGit tools (bash, ssh, ...) and Emacs.
    #
    # 2. Windows defines two 'user paths': $USERPROFILE, and the
    # concatenation of the two variables $HOMEDRIVE and $HOMEPATH. Both of
    # these paths point by default to the same location, e.g.
    # C:\Users\Username
    #
    # 3. $USERPROFILE cannot be changed, however $HOMEDRIVE and $HOMEPATH
    # can be changed. They are originally intended to be the equivalent of
    # the $HOME path, but there are many known issues with them
    #
    # 4. As for the name of the file itself, most of the tools ported from
    # Unix will use the standard '.dotfile' scheme, but some of these will
    # instead use "_dotfile". Of the latter, the two notable exceptions are
    # vim, which will first try '_vimrc' before '.vimrc' (but it will try
    # both) and git, which will require the user to name its netrc file
    # '_netrc'.
    #
    # Relevant links :
    # http://markmail.org/message/i33ldu4xl5aterrr
    # http://markmail.org/message/wbzs4gmtvkbewgxi
    # http://stackoverflow.com/questions/6031214/
    #
    # Because the whole thing is a mess, I suggest we tried various sensible
    # defaults until we succeed or have depleted all possibilities.

    if user_specified_path is not None:
        return [user_specified_path]

    if platform.system() != 'Windows':
        return [None]

    # a useful helper function that converts None to the empty string
    getenv_or_empty = lambda s: os.getenv(s) or ""

    # Now, we only treat the case of Windows
    env_vars = [["HOME"],
                ["HOMEDRIVE", "HOMEPATH"],
                ["USERPROFILE"],
                ["SYSTEMDRIVE"]]

    env_dirs = []
    for v in env_vars:
        dir = ''.join(map(getenv_or_empty, v))
        if not dir:
            logging.debug('Environment var(s) %s not defined, skipping', v)
        else:
            env_dirs.append(dir)

    additional_dirs = ["C:", ""]

    all_dirs = env_dirs + additional_dirs

    leading_chars = [".", "_"]

    res = [''.join([dir, os.sep, lc, config_name])
           for dir in all_dirs
           for lc in leading_chars]

    return res


def authenticate_through_netrc(user_specified_path=None):
    """
    Returns the tuple user / password given a path for the .netrc file
    """
    res = None
    errors = []
    paths_to_try = get_config_paths("netrc", user_specified_path)
    for p in paths_to_try:
        try:
            logging.debug('Trying netrc file %s', p)
            auths = netrc.netrc(p).authenticators('coursera-dl')
            res = (auths[0], auths[2])
            break
        except (IOError, TypeError, netrc.NetrcParseError) as e:
            errors.append(e)

    if res is None:
        for e in errors:
            logging.error(str(e))
        sys.exit(1)

    return res


def load_cookies_file(cookies_file):
    """
    Loads the cookies file.

    We pre-pend the file with the special Netscape header because the cookie
    loader is very particular about this string.
    """

    cookies = StringIO.StringIO()
    cookies.write('# Netscape HTTP Cookie File')
    cookies.write(open(cookies_file, 'r').read())
    cookies.flush()
    cookies.seek(0)
    return cookies


def get_cookie_jar(cookies_file):
    cj = cookielib.MozillaCookieJar()
    cookies = load_cookies_file(cookies_file)

    # nasty hack: cj.load() requires a filename not a file, but if I use
    # stringio, that file doesn't exist. I used NamedTemporaryFile before,
    # but encountered problems on Windows.
    cj._really_load(cookies, 'StringIO.cookies', False, False)

    return cj


def get_opener(cookies_file):
    """
    Use cookie file to create a url opener.
    """

    cj = get_cookie_jar(cookies_file)

    return urllib2.build_opener(urllib2.HTTPCookieProcessor(cj),
                                urllib2.HTTPHandler(),
                                urllib2.HTTPSHandler())


def get_page(url, cookies_file):
    """
    Download an HTML page using the cookiejar.
    """

    opener = urllib2.build_opener(urllib2.HTTPHandler(), urllib2.HTTPSHandler())
    req = urllib2.Request(url)

    opener.addheaders.append(('Cookie', 'csrf_token=%s;session=%s' % (csrftoken, session)))
    ret = opener.open(req).read()

    # opener = get_opener(cookies_file)
    # ret = opener.open(url).read()
    opener.close()
    return ret


def get_syllabus(class_name, cookies_file, local_page=False):
    """
    Get the course listing webpage.
    """

    if local_page:
        if os.path.exists(local_page):
            with open(local_page) as f:
                page = f.read()
            logging.info('Read (%d bytes) from local file', len(page))
        else:
            # we should write the local page here
            pass
    else:
        pass

    if not local_page
        url = get_syllabus_url(class_name)
        down_the_wabbit_hole(class_name, cookies_file)
        page = get_page(url, cookies_file)
        logging.info('Downloaded %s (%d bytes)', url, len(page))

        # cache the page if we're in 'local' mode
        if local_page:
            with open(local_page, 'w') as f:
                f.write(page)
    else:
        with open(local_page) as f:
            page = f.read()
        logging.info('Read (%d bytes) from local file', len(page))

    return page


def clean_filename(s):
    """
    Sanitize a string to be used as a filename.
    """

    # strip paren portions which contain trailing time length (...)
    s = re.sub("\([^\(]*$", '', s)
    s = s.strip().replace(':', '-').replace(' ', '_')
    s = s.replace('nbsp', '')
    valid_chars = '-_.()%s%s' % (string.ascii_letters, string.digits)
    return ''.join(c for c in s if c in valid_chars)


def get_anchor_format(a):
    """
    Extract the resource file-type format from the anchor
    """

    # (. or format=) then (file_extension) then (? or $)
    # e.g. "...format=txt" or "...download.mp4?..."
    fmt = re.search("(?:\.|format=)(\w+)(?:\?.*)?$", a)
    return (fmt.group(1) if fmt else None)


def parse_syllabus(page, cookies_file, reverse=False):
    """
    Parses a Coursera course listing/syllabus page.  Each section is a week
    of classes.
    """

    sections = []
    soup = BeautifulSoup(page)

    # traverse sections
    for stag in soup.findAll(attrs={'class':
                                    re.compile('^course-item-list-header')}):
        assert stag.contents[0] is not None, "couldn't find section"
        section_name = clean_filename(stag.contents[0].contents[1])
        logging.info(section_name)
        lectures = []  # resources for 1 lecture

        # traverse resources (e.g., video, ppt, ..)
        for vtag in stag.nextSibling.findAll('li'):
            assert vtag.a.contents[0], "couldn't get lecture name"
            vname = clean_filename(vtag.a.contents[0])
            logging.info('  %s', vname)
            lecture = {}

            for a in vtag.findAll('a'):
                href = a['href']
                fmt = get_anchor_format(href)
                logging.debug('    %s %s', fmt, href)
                if fmt:
                    lecture[fmt] = href

            # We don't seem to have hidden videos anymore.  University of
            # Washington is now using Coursera's standards, AFAICS.  We
            # raise an exception, to be warned by our users, just in case.
            if 'mp4' not in lecture:
                raise ClassNotFound("Missing/hidden videos?")

            lectures.append((vname, lecture))

        sections.append((section_name, lectures))

    logging.info('Found %d sections and %d lectures on this page',
                 len(sections), sum(len(s[1]) for s in sections))

    if sections and reverse:
        sections.reverse()

    if not len(sections):
        logging.error('Probably bad cookies file (or wrong class name)')

    return sections


def mkdir_p(path):
    """
    Create subdirectory hierarchy given in the paths argument.
    """

    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def download_lectures(wget_bin,
                      curl_bin,
                      aria2_bin,
                      axel_bin,
                      cookies_file,
                      class_name,
                      sections,
                      file_formats,
                      overwrite=False,
                      skip_download=False,
                      section_filter=None,
                      lecture_filter=None,
                      path='',
                      verbose_dirs=False,
                      ):
    """
    Downloads lecture resources described by sections.
    Returns True if the class appears completed.
    """
    last_update = -1

    def format_section(num, section):
        sec = '%02d_%s' % (num, section)
        if verbose_dirs:
            sec = class_name.upper() + '_' + sec
        return sec

    def format_resource(num, name, fmt):
        return '%02d_%s.%s' % (num, name, fmt)

    for (secnum, (section, lectures)) in enumerate(sections):
        if section_filter and not re.search(section_filter, section):
            logging.debug('Skipping b/c of sf: %s %s', section_filter,
                          section)
            continue
        sec = os.path.join(path, class_name, format_section(secnum + 1,
                                                            section))
        for (lecnum, (lecname, lecture)) in enumerate(lectures):
            if lecture_filter and not re.search(lecture_filter,
                                                lecname):
                continue
            if not os.path.exists(sec):
                mkdir_p(sec)

            # write lecture resources
            for fmt, url in [i for i in lecture.items() if i[0]
                             in file_formats or 'all'
                             in file_formats]:
                lecfn = os.path.join(sec, format_resource(lecnum + 1,
                                                          lecname, fmt))

                if overwrite or not os.path.exists(lecfn):
                    if not skip_download:
                        logging.info('Downloading: %s', lecfn)
                        download_file(url, lecfn, cookies_file, wget_bin,
                                      curl_bin, aria2_bin, axel_bin)
                    else:
                        open(lecfn, 'w').close()  # touch
                    last_update = time.time()
                else:
                    logging.info('%s already downloaded', lecfn)
                    # if this file hasn't been modified in a long time,
                    # record that time
                    last_update = max(last_update, os.path.getmtime(lecfn))

    # if we haven't updated any files in 1 month, we're probably
    # done with this course
    if last_update >= 0:
        if time.time() - last_update > total_seconds(datetime.timedelta(days=30)):
            logging.info('COURSE PROBABLY COMPLETE: ' + class_name)
            return True
    return False


def total_seconds(td):
    """
    Compute total seconds for a timedelta.

    Added for backward compatibility, pre 2.7.
    """
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) // 10**6


def download_file(url,
                  fn,
                  cookies_file,
                  wget_bin,
                  curl_bin,
                  aria2_bin,
                  axel_bin,
                  ):
    """
    Decides which download method to use for a given file. When the download
    is aborted by the user, the partially downloaded file is also removed.
    """

    try:
        if wget_bin:
            download_file_wget(wget_bin, url, fn)
        elif curl_bin:
            download_file_curl(curl_bin, url, fn)
        elif aria2_bin:
            download_file_aria2(aria2_bin, url, fn)
        elif axel_bin:
            download_file_axel(axel_bin, url, fn)
        else:
            download_file_nowget(url, fn, cookies_file)
    except KeyboardInterrupt:
        logging.info('Keyboard Interrupt -- Removing partial file: %s', fn)
        os.remove(fn)
        sys.exit()


def download_file_wget(wget_bin, url, fn):
    """
    Downloads a file using wget.  Could possibly use python to stream files
    to disk, but wget is robust and gives nice visual feedback.
    """

    cmd = [wget_bin, url, '-O', fn, '--no-cookies', '--header',
           "Cookie: csrf_token=%s; session=%s" % (csrftoken, session),
           '--no-check-certificate']
    logging.debug('Executing wget: %s', cmd)
    return subprocess.call(cmd)


def download_file_curl(curl_bin, url, fn):
    """
    Downloads a file using curl.  Could possibly use python to stream files
    to disk, but curl is robust and gives nice visual feedback.
    """

    cmd = [curl_bin, url, '-k', '-#', '-L', '-o', fn, '--cookie',
           "csrf_token=%s; session=%s" % (csrftoken, session)]
    logging.debug('Executing curl: %s', cmd)
    return subprocess.call(cmd)


def download_file_aria2(aria2_bin, url, fn):
    """
    Downloads a file using aria2.  Could possibly use python to stream files
    to disk, but aria2 is robust. Unfortunately, it does not give a nice
    visual feedback, bug gets the job done much faster than the
    alternatives.
    """

    cmd = [aria2_bin, url, '-o', fn, '--header',
           "Cookie: csrf_token=%s; session=%s" % (csrftoken, session),
           '--check-certificate=false', '--log-level=notice',
           '--max-connection-per-server=4', '--min-split-size=1M']
    logging.debug('Executing aria2: %s', cmd)
    return subprocess.call(cmd)


def download_file_axel(axel_bin, url, fn):
    """
    Downloads a file using axel.  Could possibly use python to stream files
    to disk, but axel is robust and it both gives nice visual feedback and
    get the job done fast.
    """

    cmd = [axel_bin, '-H', "Cookie: csrf_token=%s; session=%s" % (csrftoken, session),
           '-o', fn, '-n', '4', '-a', url]
    logging.debug('Executing axel: %s', cmd)
    return subprocess.call(cmd)


def download_file_nowget(url, fn, cookies_file):
    """
    'Native' python downloader -- slower than wget.

    For consistency with subprocess.call, returns 0 to indicate success and
    1 to indicate problems.
    """

    logging.info('Downloading %s -> %s', url, fn)
    try:
        opener = get_opener(cookies_file)
        opener.addheaders.append(('Cookie', 'csrf_token=%s;session=%s' %
                                  (csrftoken, session)))
        urlfile = opener.open(url)
    except urllib2.HTTPError:
        logging.warn('Probably the file is missing from the AWS repository...'
                     ' skipping it.')
        return 1
    else:
        bw = BandwidthCalc()
        chunk_sz = 1048576
        bytesread = 0
        with open(fn, 'wb') as f:
            while True:
                data = urlfile.read(chunk_sz)
                if not data:
                    print '.'
                    break
                bw.received(len(data))
                f.write(data)
                bytesread += len(data)
                print '\r%d bytes read%s' % (bytesread, bw),
                sys.stdout.flush()
        urlfile.close()
        return 0


def parseArgs():
    """
    Parse the arguments/options passed to the program on the command line.
    """

    parser = argparse.ArgumentParser(
        description='Download Coursera.org lecture material and resources.')

    # positional
    parser.add_argument('class_names',
                        action='store',
                        nargs='+',
                        help='name(s) of the class(es) (e.g. "nlp")')

    parser.add_argument('-c',
                        '--cookies_file',
                        dest='cookies_file',
                        action='store',
                        default=None,
                        help='full path to the cookies.txt file')
    parser.add_argument('-u',
                        '--username',
                        dest='username',
                        action='store',
                        default=None,
                        help='coursera username')
    parser.add_argument('-n',
                        '--netrc',
                        dest='netrc',
                        nargs='?',
                        action='store',
                        default=None,
                        help='use netrc for reading passwords, uses default'
                             ' location if no path specified')

    # required if username selected above
    parser.add_argument('-p',
                        '--password',
                        dest='password',
                        action='store',
                        default=None,
                        help='coursera password')

    # optional
    parser.add_argument('-f',
                        '--formats',
                        dest='file_formats',
                        action='store',
                        default='all',
                        help='file format extensions to be downloaded in'
                             ' quotes space separated, e.g. "mp4 pdf" '
                             '(default: special value "all")')
    parser.add_argument('-sf',
                        '--section_filter',
                        dest='section_filter',
                        action='store',
                        default=None,
                        help='only download sections which contain this'
                             ' regex (default: disabled)')
    parser.add_argument('-lf',
                        '--lecture_filter',
                        dest='lecture_filter',
                        action='store',
                        default=None,
                        help='only download lectures which contain this regex'
                             ' (default: disabled)')
    parser.add_argument('-w',
                        '--wget_bin',
                        dest='wget_bin',
                        action='store',
                        default=None,
                        help='wget binary if it should be used for downloading')
    parser.add_argument('--curl_bin',
                        dest='curl_bin',
                        action='store',
                        default=None,
                        help='curl binary if it should be used for downloading')
    parser.add_argument('--aria2_bin',
                        dest='aria2_bin',
                        action='store',
                        default=None,
                        help='aria2 binary if it should be used for downloading')
    parser.add_argument('--axel_bin',
                        dest='axel_bin',
                        action='store',
                        default=None,
                        help='axel binary if it should be used for downloading')
    parser.add_argument('-o',
                        '--overwrite',
                        dest='overwrite',
                        action='store_true',
                        default=False,
                        help='whether existing files should be overwritten (default: False)')
    parser.add_argument('-l',
                        '--process_local_page',
                        dest='local_page',
                        help='uses or creates local cached version of syllabus page')
    parser.add_argument('--skip-download',
                        dest='skip_download',
                        action='store_true',
                        default=False,
                        help='for debugging: skip actual downloading of files')
    parser.add_argument('--path',
                        dest='path',
                        action='store',
                        default='',
                        help='path to save the file')
    parser.add_argument('--verbose-dirs',
                        dest='verbose_dirs',
                        action='store_true',
                        default=False,
                        help='include class name in section directory name')
    parser.add_argument('--debug',
                        dest='debug',
                        action='store_true',
                        default=False,
                        help='print lots of debug information')
    parser.add_argument('--quiet',
                        dest='quiet',
                        action='store_true',
                        default=False,
                        help='omit as many messages as possible (only printing errors)')
    parser.add_argument('--add-class',
                        dest='add_class',
                        action='append',
                        default=[],
                        help='additional classes to get')
    parser.add_argument('-r',
                        '--reverse',
                        dest='reverse',
                        action='store_true',
                        default=False,
                        help='download sections in reverse order')

    args = parser.parse_args()

    # Initialize the logging system first so that other functions can use it right away
    if args.debug:
        logging.basicConfig(level=logging.DEBUG,
                            format='%(name)s[%(funcName)s] %(message)s')
    elif args.quiet:
        logging.basicConfig(level=logging.ERROR,
                            format='%(name)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO,
                            format='%(message)s')

    # turn list of strings into list
    args.file_formats = args.file_formats.split()

    # check arguments
    if args.cookies_file and not os.path.exists(args.cookies_file):
        logging.error('Cookies file not found: %s', args.cookies_file)
        sys.exit(1)

    if not args.cookies_file and not args.username:
        args.username, args.password = authenticate_through_netrc(args.netrc)

    if args.username and not args.password:
        args.password = getpass.getpass('Coursera password for %s: '
                                        % args.username)

    return args


def download_class(args, class_name):
    """
    Download all requested resources from the class given in class_name.
    Returns True if the class appears completed.
    """

    if args.username:
        tmp_cookie_file = write_cookie_file(class_name, args.username,
                                            args.password)

    # get the syllabus listing
    page = get_syllabus(class_name, args.cookies_file
                        or tmp_cookie_file, args.local_page)

    # parse it
    sections = parse_syllabus(page, args.cookies_file
                              or tmp_cookie_file, args.reverse)

    # obtain the resources
    completed = download_lectures(
                      args.wget_bin,
                      args.curl_bin,
                      args.aria2_bin,
                      args.axel_bin,
                      args.cookies_file or tmp_cookie_file,
                      class_name,
                      sections,
                      args.file_formats,
                      args.overwrite,
                      args.skip_download,
                      args.section_filter,
                      args.lecture_filter,
                      args.path,
                      args.verbose_dirs,
                      )

    if not args.cookies_file:
        os.unlink(tmp_cookie_file)

    return completed


def main():
    """
    Main entry point for execution as a program (instead of as a module).
    """

    args = parseArgs()
    completed_classes = []

    for class_name in args.class_names:
        try:
            logging.info('Downloading class: %s', class_name)
            if download_class(args, class_name):
                completed_classes.append(class_name)
        except ClassNotFound as cnf:
            logging.error('Could not find class: %s', cnf)

    if completed_classes:
        logging.info("Classes which appear completed: " + " ".join(completed_classes))


if __name__ == '__main__':
    main()
