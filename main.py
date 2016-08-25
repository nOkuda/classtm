import argparse
import numpy as np
import datetime
from email.mime.text import MIMEText
import getpass
import logging
import os
import pexpect.pxssh as pxssh
import shutil
import subprocess
import sys
import threading
import time

import ankura

from classtm import plot
from activetm import utils

'''
The output from an experiment should take the following form:

    output_directory
        settings1
            run1_1
            run2_1
            ...
        settings2
        ...

In this way, it gets easier to plot the results, since each settings will make a
line on the plot, and each line will be aggregate data from multiple runs of the
same settings.
'''


class JobThread(threading.Thread):
    def __init__(self, host, working_dir, settings, outputdir, label, password, user):
        threading.Thread.__init__(self)
        self.daemon = True
        self.host = host
        self.working_dir = working_dir
        self.settings = settings
        self.outputdir = outputdir
        self.label = label
        self.killed = False
        self.password = password
        self.user = user

    # TODO use asyncio when code gets upgraded to Python 3
    def run(self):
        s = pxssh.pxssh(timeout=600)
        hostname = self.host
        username = self.user
        password = self.password
        s.login(hostname, username, password)
        s.sendline('python3 ' + os.path.join(self.working_dir.strip(), 'submain.py') + ' ' +\
                            self.settings.strip() + ' ' +\
                            self.outputdir.strip() + ' ' +\
                            self.label.strip())
        s.prompt()
        print(s.before)
        s.logout()
        while True:
            time.sleep(1)
            if self.killed:
                s.logout()
                break


class PickleThread(threading.Thread):
    def __init__(self, host, working_dir, work, outputdir, lock, password, user):
        threading.Thread.__init__(self)
        self.daemon = True
        self.host = host
        self.working_dir = working_dir
        self.work = work
        self.outputdir = outputdir
        self.lock = lock
        self.password = password
        self.user = user

    def run(self):
        while True:
            with self.lock:
                if len(self.work) <= 0:
                    break
                else:
                    settings = self.work.pop()
                s = pxssh.pxssh(timeout=600)
                s.login(self.host, self.user, self.password)
                s.sendline('python3 ' + os.path.join(self.working_dir.strip(), 'pickle_data.py') + ' '+\
                            settings.strip() + ' ' +\
                            self.outputdir.strip())
                s.prompt()
                print(s.before)
                s.logout()


def generate_settings(filename):
    with open(filename) as ifh:
        for line in ifh:
            line = line.strip()
            if line:
                yield line


def get_hosts(filename):
    hosts = []
    with open(args.hosts) as ifh:
        for line in ifh:
            line = line.strip()
            if line:
                hosts.append(line)
    return hosts


def check_counts(hosts, settingscount):
    if len(hosts) != settingscount:
        logging.getLogger(__name__).error('Node count and settings count do not match!')
        sys.exit(1)


def get_groups(config):
    result = set()
    settings = generate_settings(config)
    for s in settings:
        d = utils.parse_settings(s)
        result.add(d['group'])
    return sorted(list(result))


def pickle_data(hosts, settings, working_dir, outputdir, password, user):
    picklings = set()
    work = set()
    for s in settings:
        pickle_name = utils.get_pickle_name(s)
        if pickle_name not in picklings:
            picklings.add(pickle_name)
            work.add(s)
    lock = threading.Lock()
    threads = []
    for h in set(hosts):
        t = PickleThread(h, working_dir, work, outputdir, lock, password, user)
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def run_jobs(hosts, settings, working_dir, outputdir, password, user):
    threads = []
    try:
        for h, s, i in zip(hosts, settings, range(len(hosts))):
            t = JobThread(h, working_dir, s, outputdir, str(i), password, user)
            t.daemon = True
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning('Killing children')
        for t in threads:
            t.killed = True
        for t in threads:
            t.join()
        runningdir = os.path.join(outputdir, 'running')
        for d in os.listdir(runningdir):
            parts = d.split('.')
            subprocess.call(['ssh', parts[0],
                'kill -s 9 ' + parts[-1] + '; exit 0'])
        sys.exit(-1)


def extract_data(fpath):
    data = []
    with open(fpath) as ifh:
        for line in ifh:
            line = line.strip()
            if line and not line.startswith('#'):
                results = line.split()
                if len(data) == 0:
                    for _ in range(len(results)):
                        data.append([])
                for i, r in enumerate(results):
                    data[i].append(float(r))
    return data


def get_data(dirname):
    data = []
    for f in os.listdir(dirname):
        fpath = os.path.join(dirname, f)
        if os.path.isfile(fpath):
            data.append(extract_data(fpath))
    return data


def get_stats(mat):
    # compute the medians along the columns
    mat_medians = np.median(mat, axis=0)
    # compute the means along the columns
    mat_means = np.mean(mat, axis=0)
    # find difference of means from first quartile along the columns
    mat_errs_minus = mat_means - np.percentile(mat, 25, axis=0)
    # compute third quartile along the columns; find difference from means
    mat_errs_plus = np.percentile(mat, 75, axis=0) - mat_means
    return mat_medians, mat_means, mat_errs_plus, mat_errs_minus


def make_plots(outputdir, dirs):
    colors = plot.get_separate_colors(len(dirs))
    dirs.sort()
    count_plot = plot.Plotter(colors)
    select_and_train_plot = plot.Plotter(colors)
    time_plot = plot.Plotter(colors)
    ymax = float('-inf')
    for d in dirs:
        data = np.array(get_data(os.path.join(outputdir, d)))
        # for the first document, read off first dimension (the labeled set
        # counts)
        counts = data[0,0,:]
        # set up a 2D matrix with each experiment on its own row and each
        # experiment's pR^2 results in columns
        ys_mat = data[:,-1,:]
        ys_medians, ys_means, ys_errs_minus, ys_errs_plus = get_stats(ys_mat)
        ys_errs_plus_max = max(ys_errs_plus + ys_means)
        if ys_errs_plus_max > ymax:
            ymax = ys_errs_plus_max
        # set up a 2D matrix with each experiment on its own row and each
        # experiment's time results in columns
        times_mat = data[:,1,:]
        times_medians, times_means, times_errs_minus, times_errs_plus = \
                get_stats(times_mat)
        count_plot.plot(counts, ys_means, d, ys_medians, [ys_errs_minus,
            ys_errs_plus])
        time_plot.plot(times_means, ys_means, d, ys_medians, [ys_errs_minus,
            ys_errs_plus], times_medians, [times_errs_minus, times_errs_plus])
        select_and_train_mat = data[:,2,:]
        sandt_medians, sandt_means, sandt_errs_minus, sandt_errs_plus = \
                get_stats(select_and_train_mat)
        select_and_train_plot.plot(counts, sandt_means, d, sandt_medians,
                [sandt_errs_minus, sandt_errs_plus])
    corpus = os.path.basename(outputdir)
    count_plot.set_xlabel('Number of Labeled Documents')
    count_plot.set_ylabel('pR$^2$')
    count_plot.set_ylim([-0.05, ymax])
    count_plot.savefig(os.path.join(outputdir, corpus+'.counts.pdf'))
    time_plot.set_xlabel('Time elapsed (seconds)')
    time_plot.set_ylabel('pR$^2$')
    time_plot.set_ylim([-0.05, ymax])
    time_plot.savefig(os.path.join(outputdir,
        corpus+'.times.pdf'))
    select_and_train_plot.set_xlabel('Number of Labeled Documents')
    select_and_train_plot.set_ylabel('Time to select and train')
    select_and_train_plot.savefig(os.path.join(outputdir,
        corpus+'.select_and_train.pdf'))


def send_notification(email, outdir, run_time):
    msg = MIMEText('Run time: '+str(run_time))
    msg['Subject'] = 'Experiment Finished for '+outdir
    msg['From'] = email
    msg['To'] = email

    p = os.popen('/usr/sbin/sendmail -t -i', 'w')
    p.write(msg.as_string())
    status = p.close()
    if status:
        logging.getLogger(__name__).warning('sendmail exit status '+str(status))


def slack_notification(msg):
    slackhook = 'https://hooks.slack.com/services/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'
    payload = 'payload={"channel": "#potatojobs", "username": "potatobot", ' +\
            '"text": "'+msg+'", "icon_emoji": ":fries:"}'
    subprocess.call([
        'curl', '-X', 'POST', '--data-urlencode', payload,
            slackhook])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Launcher for ActiveTM '
            'experiments')
    parser.add_argument('hosts', help='hosts file for job '
            'farming')
    parser.add_argument('user', help='username to login with')
    parser.add_argument('working_dir', help='ActiveTM directory '
            'available to hosts (should be a network path)')
    parser.add_argument('config', help=\
            '''a file with the path to a settings file on each line.
            The file referred to should follow the settings specification
            as discussed in README.md in the root ActiveTM directory''')
    parser.add_argument('outputdir', help='directory for output (should be a '
            'network path)')
    parser.add_argument('email', help='email address to send to when job '
            'completes', nargs='?')
    args = parser.parse_args()

    password = getpass.getpass('Password: ')

    try:
        begin_time = datetime.datetime.now()
        slack_notification('Starting job: '+args.outputdir)
        runningdir = os.path.join(args.outputdir, 'running')
        if os.path.exists(runningdir):
            shutil.rmtree(runningdir)
        try:
            os.makedirs(runningdir)
        except OSError:
            pass
        hosts = get_hosts(args.hosts)
        check_counts(hosts, utils.count_settings(args.config))
        if not os.path.exists(args.outputdir):
            logging.getLogger(__name__).error('Cannot write output to: '+args.outputdir)
            sys.exit(-1)
        groups = get_groups(args.config)
        pickle_data(hosts, generate_settings(args.config), args.working_dir,
                    args.outputdir, password, args.user)
        run_jobs(hosts, generate_settings(args.config), args.working_dir,
                 args.outputdir, password, args.user)
        make_plots(args.outputdir, groups)
        run_time = datetime.datetime.now() - begin_time
        with open(os.path.join(args.outputdir, 'run_time'), 'w') as ofh:
            ofh.write(str(run_time))
        os.rmdir(runningdir)
        slack_notification('Job complete: '+args.outputdir)
        if args.email:
            send_notification(args.email, args.outputdir, run_time)
    except:
        slack_notification('Job died: '+args.outputdir)
        raise

