#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Created on 8 October 2018

@author: Philip Winkler

Copyright © 2018 Philip Winkler, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms of the GNU
General Public License version 2 as published by the Free Software Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even
the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
Public License for more details.

You should have received a copy of the GNU General Public License along with Odemis. If not,
see http://www.gnu.org/licenses/.

"""

from __future__ import division, absolute_import

import logging
import os
import re
import shlex
import time
import wx
import csv
import zipfile
import socket
from datetime import datetime
from concurrent import futures
import platform
from glob import glob
import subprocess
import json
import base64
import urllib2
from collections import OrderedDict
import webbrowser

logging.getLogger().setLevel(logging.DEBUG)
DEFAULT_CONFIG = {"LOGLEVEL": "1",
                  "TERMINAL": "/usr/bin/gnome-terminal"}

OS_TICKET_URL = "https://support.delmic.com/api/tickets.json"
TEST_SUPPORT_TICKET = (os.environ.get("TEST_SUPPORT_TICKET", 0) != 0)
RESPONSE_SUCCESS = 201
MAX_USERS = 50
GDPR_TEXT = ("When reporting an issue, technical data from the computer will be sent " +
    "to Delmic B.V. In addition to your name and email address, the data can " +
    "also contain some identifiable information about your work (e.g, " +
    "filenames of acquisitions). The sole purpose of collecting this data " +
    "is to diagnose issues and improve the quality of the system. The data may " +
    "be stored up to five years. The data will always be stored confidentially, " +
    "and never be shared with any third parties or used for any commercial purposes.")
DESCRIPTION_DEFAULT_TXT = ("Ways to reproduce the problem:\n1.\n2.\n3.\n\nCurrent behaviour:\n\n" +
    "Expected behaviour:\n\nAdditional Information (e.g. reproducibility, severity):\n")
# Constants for checking output of odemis-cli --check. Don't import from odemis.util.driver
# to keep the bugreporter as independent as possible from odemis.
BACKEND_RUNNING = 0
BACKEND_STOPPED = 2
BACKEND_STARTING = 3


# The next to functions will be needed to parse odemis.conf
def _add_var_config(config, var, content):
    """ Add one variable to the config, handling substitution

    Args:
        config: (dict) Configuration to add the found values to
        var: (str) The name of the variable
        content: (str) Value of the variable

    Returns:
        dict: The `config` dictionary is returned with the found values added

    """

    # variable substitution
    m = re.search(r"(\$\w+)", content)
    while m:
        subvar = m.group(1)[1:]
        # First try to use a already known variable, and fallback to environ
        try:
            subcont = config[subvar]
        except KeyError:
            try:
                subcont = os.environ[subvar]
            except KeyError:
                logging.warning("Failed to find variable %s", subvar)
                subcont = ""
        # substitute (might do several at a time, but it's fine)
        content = content.replace(m.group(1), subcont)
        m = re.search(r"(\$\w+)", content)

    logging.debug("setting %s to %s", var, content)
    config[var] = content

def parse_config(configfile):
    """  Parse `configfile` and return a dictionary of its values

    The configuration file was originally designed to be parsed as a bash script. So each line looks
    like:

        VAR=$VAR2/log

    Args:
        configfile: (str) Path to the configuration file

    Returns:
        dict str->str: Config file as name of variable -> value

    """

    config = DEFAULT_CONFIG.copy()
    f = open(configfile)
    for line in shlex.split(f, comments=True):
        tokens = line.split("=")
        if len(tokens) != 2:
            logging.warning("Can't parse '%s', skipping the line", line)
        else:
            _add_var_config(config, tokens[0], tokens[1])

    return config


class OdemisBugreporter():
    """
    Class to create a bugreport. Contains functions for compressing the odemis files, opening
    a window asking for a bugreport description, and uploading the bugreport to
    osticket.
    """
    def __init__(self):

        self.zip_fn = None  # (str) Path to zip file.
        self._executor = futures.ThreadPoolExecutor(max_workers=4)

    def run(self):
        """
        Runs the compression and ticket creation in a separate thread. Starts the GUI.
        The create_ticket function waits until the compression is done and the user 
        has finished the report description before sending.
        """
        # Take a screenshot if the GUI is there
        ret_code = subprocess.call(['pgrep', '-f', 'odemis.gui.main'])
        if ret_code == 0:
            scfn = "/tmp/odemis-bug-screenshot.png"
            try:
                if int(platform.linux_distribution()[1][:2]) > 14:
                    # Only available in Ubuntu 14.04+ (a bit better because you see a "flash")
                    subprocess.call(['gnome-screenshot', '-f', scfn])
                else:
                    subprocess.call(['gm', 'import', '-window', 'root', scfn])
            except Exception as e:
                logging.warning("Failed to take a screenshot with Exception %s" % e)

        # Compress files in the background and set up ticket creation
        self._compress_files_f = self._executor.submit(self.compress_files)

        # Ask for user description in GUI
        app = wx.App()
        self.gui = BugreporterFrame(self)
        app.MainLoop()

        self._executor.shutdown()

    def create_ticket(self, api_key, fields, files=None):
        """
        Create ticket on osTicket server.
        :arg api_key: (String) API-Key
        :arg fields: (String --> String) dictionary containing keys name, email, subject, message
        :arg files: (None or list of Strings) pathname of zip files that should be attached
        :returns: (int) response code
        :raises ValueError: ticket upload failed
        :raises urllib2.HTTPError: key not accepted
        :raises urllib2.URLError: connection problem
        """
        if not files:
            files = []
        fields["attachments"] = []
        for fn in files:
            with open(fn, "rb") as f:
                encoded_data = base64.b64encode(f.read())
            att_desc = {str(fn): "data:application/zip;base64,%s" % encoded_data}
            fields["attachments"].append(att_desc)

        description = json.dumps(fields)
        req = urllib2.Request(OS_TICKET_URL, description, headers={"X-API-Key": api_key})
        f = urllib2.urlopen(req)
        response = f.getcode()
        f.close()
        if response == RESPONSE_SUCCESS:
            return
        else:
            raise ValueError('Ticket creation failed with error code %s.' % response)
        
    def search_api_key(self):
        """
        Searches for a valid osticket key on the system. First, the customer key
        is checked, then the fallback.
        """
        customer_key_path = os.path.join(os.path.expanduser(u"~"), '.local', 'share',
                                         'odemis', 'osticket.key')
        fallback_key_path = os.path.join('usr', 'share', 'odemis', 'osticket.key')
        if os.path.isfile(customer_key_path):
            with open(customer_key_path, 'r') as key_file:
                api_key = key_file.read().strip('\n')
        elif os.path.isfile(fallback_key_path):
            with open(fallback_key_path, 'r') as key_file:
                api_key = key_file.read().strip('\n')
        else:
            raise LookupError("osTicket key not found.")
        return api_key

    def compress_files(self):
        """
        Compresses the relevant files to a zip archive which is saved in /home.
        :modifies self.zip_fn: filename of the zip archive
        """
        hostname = socket.gethostname()
        home_dir = os.path.expanduser(u"~")
        t = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.zip_fn = os.path.join(home_dir, 'Desktop', '%s-odemis-log-%s.zip' % (hostname, t))

        logging.debug("Will store bug report in %s", self.zip_fn)
        files = ['/var/log/odemis.log', os.path.join(home_dir, 'odemis-gui.log'),
                 os.path.join(home_dir, 'odemis-gui.log.1'), '/etc/odemis.conf', '/var/log/syslog',
                 os.path.join(home_dir, 'odemis-mic-selector.log'), '/tmp/odemis-bug-screenshot.png',
                 '/etc/odemis-settings.yaml']

        try:
            # Save yaml file, call MODEL_SELECTOR if needed
            odemis_config = parse_config("/etc/odemis.conf")
            models = []
            if odemis_config.get("MODEL"):
                models = [odemis_config["MODEL"]]
            elif odemis_config.get("MODEL_SELECTOR"):
                logging.debug("Calling %s", odemis_config["MODEL_SELECTOR"].rstrip().split(' '))
                try:
                    cmd = shlex.split(odemis_config["MODEL_SELECTOR"])
                    logging.debug("Getting the model filename using %s", cmd)
                    out = subprocess.check_output(cmd).splitlines()
                    if out:
                        models = [out[0].strip()]
                    else:
                        logging.warning("Model selector failed to pick a model")
                except Exception as ex:
                    logging.warning("Failed to run model selector: %s", ex)

            if not models:
                # just pick every potential microscope model
                models = glob(os.path.join(odemis_config['CONFIGPATH'], '*/*.odm.yaml'))
            files.extend(models)

            # Add the latest overlay-report if it's possibly related (ie, less than a day old)
            overlay_reps = glob(os.path.join(home_dir, 'odemis-overlay-report', '*'))
            overlay_reps.sort(key=os.path.getmtime)
            if overlay_reps and (time.time() - os.path.getmtime(overlay_reps[-1])) / 3600 < 24:
                files.append(overlay_reps[-1])

            # Add the latest DELPHI calibration report if it's possibly related (ie, less than a day old)
            delphi_calib_reps = glob(os.path.join(home_dir, 'delphi-calibration-report', '*'))
            delphi_calib_reps.sort(key=os.path.getmtime)
            if delphi_calib_reps and (time.time() - os.path.getmtime(delphi_calib_reps[-1])) / 3600 < 24:
                files.append(delphi_calib_reps[-1])

            # Save hw status (if available)
            try:
                ret_code = subprocess.call(['odemis-cli', '--check'])
            except Exception as ex:
                logging.warning("Failed to run check backend status: %s", ex)
                ret_code = BACKEND_STOPPED
            if ret_code in (BACKEND_RUNNING, BACKEND_STARTING):
                try:
                    # subprocess doesn't have timeout argument in python 2.x, so use future instead
                    f = self._executor.submit(subprocess.check_output, ['odemis-cli', '--list-prop', '*'])
                    props = f.result(60)
                    hwfn = "/tmp/odemis-hw-status.txt"
                    with open(hwfn, 'w+') as f:
                        f.write(props)
                    files.append(hwfn)
                except Exception as ex:
                    logging.warning("Cannot save hw status: %s", ex)

            # Compress files
            with zipfile.ZipFile(self.zip_fn, "w", zipfile.ZIP_DEFLATED) as archive:
                for f in files:
                    if os.path.isfile(f):
                        logging.debug("Adding file %s", f)
                        archive.write(f, os.path.basename(f))
                    elif os.path.isdir(f):
                        logging.debug("Adding directory %s", f)
                        dirnamef = os.path.dirname(f)
                        for top, _, files in os.walk(f):
                            for subf in files:
                                full_path = os.path.join(top, subf)
                                archive.write(full_path, full_path[len(dirnamef) + 1:])
                    else:
                        logging.warning("Bugreporter could not find file %s", f)
        except Exception:
            logging.exception("Failed to store bug report")
            raise

    def _set_description(self, name, email, subject, message):
        """
        Saves the description parameters for the ticket creation in a txt file, compresses
        the file and calls self._create_ticket.
        :arg name, email, summary, description: (String) arguments for corresponding dictionary
        keys
        """
        self._compress_files_f.result()
        report_description = {'name': name.encode("utf-8"),
                              'email': email.encode("utf-8"),
                              'subject': subject.encode("utf-8"),
                              'message': message.encode("utf-8")}
        # Create ticket with special id when testing
        if TEST_SUPPORT_TICKET:
            report_description['topicId'] = 12

        description = ('Name: %s\n' % name.encode("utf-8") +
                       'Email: %s\n' % email.encode("utf-8") +
                       'Summary: %s\n\n' % subject.encode("utf-8") +
                       'Description:\n%s' % message.encode("utf-8")
                       )

        with zipfile.ZipFile(self.zip_fn, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('description.txt', description)
        wx.CallAfter(self.gui.wait_lbl.SetLabel, "Sending report...")
        try:
            api_key = self.search_api_key()
            self.create_ticket(api_key, report_description, [self.zip_fn])
            wx.CallAfter(self.gui.Destroy)
        except Exception as e:
            logging.warning("osTicket upload failed: %s", e)
            wx.CallAfter(self.gui.open_failed_upload_dlg)

    def send_report(self, name, email, subject, message):
        """
        Calls _set_description in a thread.
        :arg name, email, summary, description: (String) arguments for corresponding dictionary
        keys
        """
        self._executor.submit(self._set_description, name, email, subject, message)


class BugreporterFrame(wx.Frame):

    def __init__(self, controller):
        super(BugreporterFrame, self).__init__(None, title="Odemis problem description", size=(800, 800))

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Add input fields for name, email, summary and description
        name_sizer = wx.BoxSizer(wx.HORIZONTAL)
        name_lbl = wx.StaticText(panel, wx.ID_ANY, "Name:")
        name_ctrl = wx.TextCtrl(panel, wx.ID_ANY, size=(500, 23))
        name_ctrl.Bind(wx.EVT_TEXT, self.on_name_text)
        name_ctrl.Bind(wx.EVT_KEY_DOWN, self.on_name_key_down)
        name_ctrl.Bind(wx.EVT_KILL_FOCUS, self.on_name_focus)
        name_sizer.Add(name_lbl, 5, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 10)
        name_sizer.Add(name_ctrl, 10, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 5)
        sizer.Add(name_sizer)
        email_sizer = wx.BoxSizer(wx.HORIZONTAL)
        email_lbl = wx.StaticText(panel, wx.ID_ANY, "Email:")
        email_ctrl = wx.TextCtrl(panel, wx.ID_ANY, size=(500, 23))
        email_sizer.Add(email_lbl, 5, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 10)
        email_sizer.Add(email_ctrl, 10, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 5)
        sizer.Add(email_sizer)
        summary_sizer = wx.BoxSizer(wx.HORIZONTAL)
        summary_lbl = wx.StaticText(panel, wx.ID_ANY, "Summary:")
        summary_ctrl = wx.TextCtrl(panel, wx.ID_ANY, size=(500, 23))
        summary_sizer.Add(summary_lbl, 5, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 10)
        summary_sizer.Add(summary_ctrl, 10, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 5)
        sizer.Add(summary_sizer)
        description_sizer1 = wx.BoxSizer(wx.HORIZONTAL)
        description_sizer2 = wx.BoxSizer(wx.HORIZONTAL)
        description_lbl = wx.StaticText(panel, wx.ID_ANY, "Description:")
        description_ctrl = wx.TextCtrl(panel, wx.ID_ANY, value=DESCRIPTION_DEFAULT_TXT,
                                       size=(self.GetSize()[0], 400), style=wx.TE_MULTILINE)
        description_sizer1.Add(description_lbl, 5, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 10)
        description_sizer2.Add(description_ctrl, 5, wx.EXPAND | wx.ALIGN_LEFT | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(description_sizer1)
        sizer.Add(description_sizer2)

        # GDPR text
        gdpr_sizer = wx.BoxSizer(wx.HORIZONTAL)
        gdpr_lbl = wx.StaticText(panel, -1, GDPR_TEXT)
        gdpr_lbl.Wrap(gdpr_lbl.GetSize().width)
        font = wx.Font(10, wx.NORMAL, wx.ITALIC, wx.NORMAL)
        gdpr_lbl.SetFont(font)
        gdpr_sizer.Add(gdpr_lbl, 10, wx.EXPAND | wx.ALIGN_LEFT | wx.ALL, 10)
        sizer.Add(gdpr_sizer, wx.EXPAND)

        # Cancel and send report buttons
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        cancel_btn = wx.Button(panel, wx.ID_ANY, "Cancel")
        cancel_btn.Bind(wx.EVT_BUTTON, self.on_close)
        button_sizer.Add(cancel_btn, 0, wx.ALL, 10)
        report_btn = wx.Button(panel, wx.ID_ANY, "Report")
        report_btn.Bind(wx.EVT_BUTTON, self.on_report_btn)
        button_sizer.Add(report_btn, 0, wx.ALL, 10)

        # Status update label
        # TODO: replace by an animated throbber
        wait_lbl = wx.StaticText(panel, wx.ID_ANY, "")
        sizer.Add(wait_lbl, 10, wx.EXPAND | wx.ALL, 10)
        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        panel.SetSizer(sizer)

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Centre()
        self.Show()
        self.Layout()

        # Make these elements class attributes to easily access the contents
        self.panel = panel
        self.name_ctrl = name_ctrl
        self.email_ctrl = email_ctrl
        self.summary_ctrl = summary_ctrl
        self.description_ctrl = description_ctrl
        self.wait_lbl = wait_lbl

        self.bugreporter = controller

        # flag is set to False when the backspace is pressed
        self.make_suggestion = True

        # Load known users, if not available make tsv file
        self.conf_path = os.path.join(os.path.expanduser(u"~"), '.config', 'odemis', 'bugreporter_users.tsv')
        with open(self.conf_path, 'a+') as f:
            reader = csv.reader(f, delimiter='\t')
            self.known_users = OrderedDict()
            for name, email in reader:
                self.known_users[name.decode("utf-8")] = email.decode("utf-8")

    def store_user_info(self, name, email):
        """
        Store the user name and email in the config file, so it can be suggested the
        next time the bugreporter is used.
        :arg name: (String) user name
        :arg email: (String) user email
        """
        # Add user to top of tsv file, truncate file if it contains too many users.
        # Adding the user to the top of the list ensures that the suggestion is made based
        # on the latest entry. Otherwise, if a typo occurred the first time the name was written,
        # the faulty name will always be suggested.
        if name in self.known_users.keys():
            del self.known_users[name]
        elif len(self.known_users.items()) >= MAX_USERS:
            oldest_entry = self.known_users.keys()[-1]
            del self.known_users[oldest_entry]
        # It would be nicer not to create a new ordered dictionary, but to move the
        # element internally. The python 3 version has such a function (move_to_end).
        prev_items = self.known_users.items()
        self.known_users = OrderedDict([(name, email)] + prev_items)
        # Overwrite tsv file
        with open(self.conf_path, 'w+') as f:
            writer = csv.writer(f, delimiter='\t')
            writer.writerows([(name.encode("utf-8"), email.encode("utf-8")) \
                              for name, email in self.known_users.items()])

    def on_name_key_down(self, evt):
        """
        If the pressed key is the backspace, delete suggestion and allow user to type new key.
        """
        if evt.GetKeyCode() in (wx.WXK_BACK, wx.WXK_DELETE):
            self.make_suggestion = False
        else:
            # If the user adds/replaces at the end of the text => auto-complete
            ip = self.name_ctrl.GetInsertionPoint()
            sel = self.name_ctrl.GetSelection()
            if sel[0] != sel[1]:
                ip = sel[1]
            self.make_suggestion = (ip == self.name_ctrl.GetLastPosition())

        evt.Skip()

    def on_name_text(self, evt):
        """
        Suggest a username from the configuration file.
        """
        if self.make_suggestion:
            full_text = evt.String
            if not full_text:
                return

            # Get typed text from input field (don't include suggestion)
            sel = self.name_ctrl.GetSelection()
            if sel[0] != sel[1]:
                typed = full_text[:len(full_text) - sel[0] + 1]
            else:
                typed = full_text

            # Suggest name from configuration file and select suggested part
            # Note about the use of wx.CallAfter: For some reason, ChangeValue causes a
            # text event to be triggered even though it's not supposed to. Through
            # the use of CallAfter, this behaviour is avoided. It is still
            # not clear what the reason for this is, but it seems to work.
            for name in self.known_users.keys():
                if name.upper().startswith(typed.upper()):
                    wx.CallAfter(self.name_ctrl.ChangeValue, name)
                    break
            else:
                wx.CallAfter(self.name_ctrl.ChangeValue, typed)
            wx.CallAfter(self.name_ctrl.SetSelection, len(typed), -1)

    def on_name_focus(self, _):
        """
        Suggest email address for username.
        """
        name = self.name_ctrl.GetValue()
        if name in self.known_users.keys():
            self.email_ctrl.ChangeValue(self.known_users[name])

    def on_report_btn(self, _):
        """
        Disable all widgets, send ticket, save name and email in configuration file.
        """
        name = self.name_ctrl.GetValue()
        email = self.email_ctrl.GetValue()
        summary = self.summary_ctrl.GetValue()
        description = self.description_ctrl.GetValue()
        
        if not name or not email or not summary or description == DESCRIPTION_DEFAULT_TXT:
            dlg = wx.MessageDialog(self, 'Please fill in all the fields.', '', wx.OK)
            val = dlg.ShowModal()
            dlg.Show()
            if val == wx.ID_OK:
                dlg.Destroy()
                return

        self.wait_lbl.SetLabel("Compressing files...")
        self.Layout()

        for widget in self.panel.GetChildren():
            widget.Enable(False)
        self.wait_lbl.Enable(True)

        # Store user info and pass description to bugreporter
        self.store_user_info(name, email)
        self.bugreporter.send_report(name, email, summary, description)

    def on_close(self, _):
        """
        Ask user for confirmation if window was opened more than 30 seconds.
        """
        # Ask for confirmation if the user has already filled in a summary or description
        if self.summary_ctrl.GetValue() or self.description_ctrl.GetValue() != DESCRIPTION_DEFAULT_TXT:
            dlg = wx.MessageDialog(self, 'The report has not been sent. Do you want to quit?',
                                   '', wx.OK | wx.CANCEL)
            val = dlg.ShowModal()
            dlg.Show()
            if val == wx.ID_CANCEL:
                dlg.Destroy()
                for widget in self.panel.GetChildren():
                    widget.Enable(True)
            elif val == wx.ID_OK:
                self.Destroy()
        else:
            self.Destroy()

    def open_failed_upload_dlg(self):
        """
        Ask the user to user wetransfer in case the upload to osticket failed.
        """
        txt = ('The bugreport could not be uploaded to osticket. Please finish the report ' +
               'by filling in the form on https://support.delmic.com and attaching the report ' +
               'file "%s" on the Desktop.\n\n' % self.bugreporter.zip_fn +
               'After closing this window, the form will automatically open in your web browser.')
        dlg = wx.MessageDialog(self, txt, '', wx.OK)
        val = dlg.ShowModal()
        dlg.Show()
        if val == wx.ID_OK:
            self.Destroy()
            webbrowser.open('https://support.delmic.com/open.php')


if __name__ == '__main__':
    bugreporter = OdemisBugreporter()
    bugreporter.run()
    
