import argparse
import numpy as np
import datetime
from email.mime.text import MIMEText
import getpass
import logging
import os
import pexpect.pxssh as pxssh
import pickle
import shutil
import subprocess
import sys
import threading
import time

import ankura
import classtm.models

from classtm import plot
from classtm import labeled
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
        self.sshclient = pxssh.pxssh(timeout=None)

    # TODO use asyncio when code gets upgraded to Python 3
    def run(self):
        s = self.sshclient
        try:
            s.login(self.host, self.user, self.password)
            if self.killed:
                return
            s.sendline('python3 ' + os.path.join(self.working_dir, 'submain.py') + ' ' +\
                                self.settings + ' ' +\
                                self.outputdir + ' ' +\
                                self.label)
            while True:
                if self.killed:
                    return
                if s.prompt(timeout=1):
                    break
            print(s.before)
            s.logout()
        except pxssh.ExceptionPxssh as e:
            print('pxssh failed to login on host ' + self.host)
            print(e)


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
                s = pxssh.pxssh(timeout=None)
                try:
                    s.login(self.host, self.user, self.password)
                    s.sendline('python3 ' + os.path.join(self.working_dir, 'pickle_data.py') + ' '+\
                                settings + ' ' +\
                                self.outputdir)
                    s.prompt()
                    print(s.before)
                    s.logout()
                except pxssh.ExceptionPxssh as e:
                    print('pxssh failed to login on host ' + self.host)
                    print(e)


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
            if t.sshclient.isalive():
                t.sshclient.close(force=True)
        runningdir = os.path.join(outputdir, 'running')
        for d in os.listdir(runningdir):
            parts = d.split('.')
            logging.getLogger(__name__).warning('killing job ' + parts[-1] + ' on host ' + parts[0])
            p = pxssh.pxssh()
            try:
                p.login(parts[0], user, password)
                p.sendline('kill -s 9 ' + parts[-1])
                p.prompt()
                logging.getLogger(__name__).warning(p.before)
                p.logout()
            except pxssh.ExceptionPxssh as e:
                logging.getLogger(__name__).warning('pxssh failed to login on host ' + parts[0])
                logging.getLogger(__name__).warning(e)
        for t in threads:
            t.join()
        sys.exit(-1)


def extract_data(fpath):
    with open(fpath, 'rb') as ifh:
        return pickle.load(ifh)


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


def get_accuracy(datum):
    true_pos = datum['confusion_matrix']['pos']['pos']
    true_neg = datum['confusion_matrix']['neg']['neg']
    false_pos = datum['confusion_matrix']['neg']['pos']
    false_neg = datum['confusion_matrix']['pos']['neg']

    d_true = 0
    d_false = 0
    d_true += true_pos
    d_true += true_neg
    d_false += false_pos
    d_false += false_neg
    accuracy = d_true / (d_true + d_false)
    return accuracy


def make_plots(outputdir, dirs):
    colors = plot.get_separate_colors(len(dirs))
    dirs.sort()
    accuracy_plot = plot.Plotter(colors)
    free_accuracy = []
    sup_accuracy = []
    num_topics = [20, 40, 60, 80]
    for d in dirs:
        # pull out the data
        data = get_data(os.path.join(outputdir, d))
        eval_times = []
        init_times = []
        train_times = []
        models = []
        for datum in data:
            if type(datum['model']) is classtm.models.FreeClassifyingAnchor:
                free_accuracy.append(get_accuracy(datum))
            elif type(datum['model']) is classtm.models.LogisticAnchor:
                sup_accuracy.append(get_accuracy(datum))

    # plot the data
    accuracy_plot.plot(num_topics, free_accuracy, 'Free Classifier', free_accuracy, yerr=None)
    accuracy_plot.plot(num_topics, sup_accuracy, 'Supervised Classifier', sup_accuracy, yerr=None)
    accuracy_plot.set_xlabel('Number of Topics')
    accuracy_plot.set_ylabel('Accuracy')
    accuracy_plot.set_ylim([min(min(free_accuracy), min(sup_accuracy)),
                            max(max(free_accuracy), max(sup_accuracy))])
    accuracy_plot.savefig(os.path.join(outputdir, 'accuracy.pdf'))


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

    print('Please enter username and password to ssh with')
    user = input('Username: ')
    password = getpass.getpass('Password: ')

    try:
        begin_time = datetime.datetime.now()
#        slack_notification('Starting job: '+args.outputdir)
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
#        pickle_data(hosts, generate_settings(args.config), args.working_dir,
#                    args.outputdir, password, user)
#        run_jobs(hosts, generate_settings(args.config), args.working_dir,
#                 args.outputdir, password, user)
        make_plots(args.outputdir, groups)
        run_time = datetime.datetime.now() - begin_time
        with open(os.path.join(args.outputdir, 'run_time'), 'w') as ofh:
            ofh.write(str(run_time))
        os.rmdir(runningdir)
#        slack_notification('Job complete: '+args.outputdir)
        if args.email:
            send_notification(args.email, args.outputdir, run_time)
    except:
#        slack_notification('Job died: '+args.outputdir)
        raise

