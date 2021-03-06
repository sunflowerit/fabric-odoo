# -*- coding: utf-8 -*-

import io
import json
import os
import sys
import time

from os.path import expanduser
from StringIO import StringIO
import subprocess 

from fabric.context_managers import *
from fabric.contrib.console import confirm
from fabric.contrib.files import append, exists, upload_template
from fabric.api import *


class FabricException(Exception):
    pass


class OdooInstance:
    
    def __init__(self, instance=False):
        self.instance = instance
        self.username = "odoo-" + self.instance
        self.home = "/home/{}".format(self.username)
        self.odooconfigfile = "{}/odooconfig.json".format(self.home)
        env.shell = "/bin/bash -c"
        env.sudo_prefix = 'sudo -H -S -p \'{}\''.format(env.sudo_prompt)

    def send_config_to_mail(self, url=False, version=False, email=False):
	msg = "\
            Dear User, below are details of you newly created odoo instance:\n\n\
            name: {0}.\n\
            url: {1}.\n\
            version: {2}\n\
            username: admin.\n\
            password: admin.\n\n\
            You can change the password after login.\n\n\
            Regards,\n\
            Sunflower IT.".format(self.instance, url, version)
        script = "echo '{}' | mail -s 'subject' nza.terrence@gmail.com".format(msg)
        p = subprocess.Popen(script, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)        
        p.communicate()[0]

    def configure_unix_user(self):
        if not self.unix_user_exists():
            print "Creating Unix User..."
            self.create_unix_user()
        print "Setting up Unix User..."
        self.setup_unix_user()

    def rebuild_odoo(self):
        self.create_local_cfg()
        self.run_buildout()
        self.stop_odoo()
        self.upgrade_odoo()
        self.restart_odoo()

    def reload_config_from_remote(self):
        fd = StringIO()
        get(self.odooconfigfile, fd)
        content=fd.getvalue()
        print content
        config_dict = json.loads(content)
        self.dbuser = config_dict.get('postgres_user')
        self.password = config_dict.get('postgres_password')
        self.port = config_dict.get('port')

    def install_odoo(self, url=False, version=False, email=False):
        # set vars
        self.url = url or self.instance + '.1systeem.nl'
        self.password = self.get_password()
        self.port = self.get_port()
        self.logfile = "{}/{}.log".format(self.home, self.username)
        self.dbuser = "odoo" + self.instance.replace('-','')
        self.nginx_file_name = "odoo_" + self.instance
        self.email = email
        try:
            v = int(version)
            self.branch = '{0}.0-custom-standard'.format(version)
        except ValueError:
            self.branch = version
        print self.branch

        # do installation steps
        with cd('/tmp'):
            self.add_host_to_ssh_config()
            self.ssh_git_clone()
            self.setup_postgres_user()
            self.add_restart()
            self.add_sudo()
            self.add_odoo_service()
            self.encrypt_https_certificate()
            self.configure_nginx()
            self.create_config_file()
            self.send_config_to_mail()
            self.after_installation()

            # run buildout, upgrade and restart odoo.
            self.rebuild_odoo()
    
    def get_password(self):
        return sudo("pwgen | awk '{print $1;}'")

    def create_unix_user(self):
        """ Create unix user """
        sudo("adduser {} --disabled-password --gecos GECOS".format(self.username))

    def check_exist(self):
        if exists(self.home):
            if not confirm('instance {} already exists in {}. Continue?'.format(
                    self.instance, self.home)):
                sys.exit(1)

    def setup_unix_user(self):
        ssh_dir = "{}/.ssh".format(self.home)
        sudo("mkdir -p {}".format(ssh_dir))
        sudo("chown -R {0}:{0} {1}".format(self.username, self.home))
        sudo("chmod 700 {}".format(ssh_dir))

        auth_file = "{}/authorized_keys".format(ssh_dir)
        put('config/authorized_keys', auth_file, use_sudo=True)
        sudo("chmod 600 {}".format(auth_file))
        sudo("chown {0}:{0} {1}".format(self.username, auth_file))

        vim_file = "{}/.vimrc".format(self.home)
        put('templates/.vimrc', vim_file, use_sudo=True)
        sudo("chown {0}:{0} {1}".format(self.username, vim_file))

        aliases = "{}/.bash_aliases".format(self.home)
        put('templates/.bash_aliases', aliases, use_sudo=True)
        sudo("chown {0}:{0} {1}".format(self.username, vim_file))

        with settings(sudo_user=self.username), cd(self.home):
            sudo("mkdir -p {}".format(ssh_dir))
            sudo("chmod 700 {}".format(ssh_dir))

            sudo('pip install --upgrade --user https://github.com/sunflowerit/dev-helper-scripts/archive/master.zip')
            profile = self.home + '/.profile'
            bash_profile = self.home + '/.bash_profile'
            if exists(bash_profile) or not exists(profile):
                profile = bash_profile
            append(profile, 'if [ -d "$HOME/.local/bin" ] ; then PATH="$HOME/.local/bin:$PATH"; fi', use_sudo=True)
            sudo('git config --global user.name "Odoo instance: {}"'.format(self.instance))
            sudo('git config --global user.email info@sunflowerweb.nl')

        print('Unix User Setup Successful...')

    def setup_postgres_user(self):
        with settings(sudo_user='postgres'), cd('/tmp'):
            if not self.postgres_user_exists():
                sudo("createuser {}".format(self.dbuser))
            sudo(
                "psql postgres -tAc \"ALTER USER {DBUSER} WITH PASSWORD '{PASSWORD}'\" && "
                "psql postgres -tAc \"ALTER USER {DBUSER} CREATEDB\""
                .format(
                    DBUSER=self.dbuser,
                    PASSWORD=self.password,
                )
            )
            print('Postgres User Setup Successful...')

    def unix_user_exists(self):
        with settings(
            hide('warnings', 'running', 'stdout', 'stderr'),
            warn_only=True
        ):
            check_user = sudo(
                "id -u {USERNAME}".format(
                    USERNAME=self.username
                )
            )
            if "no such user" in check_user: 
                print('Unix User does not exist')
                return False
            else:
                print('Unix User exists')
                return True

    def postgres_user_exists(self):
        with settings(
            hide('warnings', 'running', 'stdout', 'stderr'),
            warn_only=True,
            sudo_user='postgres'
        ), cd('/tmp'):
            return sudo(
                "psql postgres -tAc \"SELECT 1 FROM pg_roles WHERE rolname='{}'\"".format(self.dbuser)
            )
           
    def add_host_to_ssh_config(self):
        home = expanduser("~")
        config_file = "{}/.ssh/config".format(home)

        # CHECK IF HOST EXISTS IN ./ssh/config
        host_exists = False
        searchfile = open(config_file, "w+")
        for line in searchfile:
            if self.username in line: 
                host_exists = self.username
        searchfile.close()

        #ADD HOST TO ./ssh/config
        if not host_exists:
            with open(config_file, "a") as config:
                config.write("""
                Host {0}\n
                ForwardAgent yes\n
                HostName {1}\n
                User {0}\n
                """.format(self.username, env.host))

    def ssh_git_clone(self):
        known_hosts = sudo("find /home/{}/.ssh -name known_hosts".format(self.username))
        buildout = sudo("find /home/{} -type d -name buildout".format(self.username))
        if not known_hosts:
            print "Known Hosts does not exist, adding file known_hosts..."
            os.system("ssh {0} 'touch /home/{0}/.ssh/known_hosts'".format(self.username))
            os.system("ssh {0} 'ssh-keygen -F github.com || ssh-keyscan github.com >> /home/{0}/.ssh/known_hosts'".format(self.username))
        with cd(self.home):
            if not buildout:
                print "Buildout does not exist, cloning into home dir...", self.username
                os.system("ssh {} 'git clone git@github.com:sunflowerit/custom-installations.git --branch {} --single-branch buildout'".format(self.username, self.branch))
            else:
                os.system("ssh {} 'git -C buildout fetch origin {}'".format(self.username, self.branch))
                os.system("ssh {} 'git -C buildout branch -D dummy'".format(self.username))
                os.system("ssh {} 'git -C buildout checkout -b dummy'".format(self.username))
                os.system("ssh {} 'git -C buildout branch -D {}'".format(self.username, self.branch))
                os.system("ssh {} 'git -C buildout branch -a'".format(self.username))
                os.system("ssh {} 'git -C buildout checkout -b {} FETCH_HEAD'".format(self.username, self.branch))
                os.system("ssh {} 'git -C buildout branch -a'".format(self.username))

    def run_buildout(self):
        os.system("ssh {} 'cd $HOME/buildout && ./bootstrap'".format(self.username))
        os.system("ssh {} 'cd $HOME/buildout && ./buildout'".format(self.username))

    def add_odoo_service(self):
        service = sudo("find /lib/systemd/system/ -name {0}.service".format(self.username))
        if not service:
            servicefile = '/lib/systemd/system/{}.service'.format(self.username)
            upload_template(
                'templates/odoo.service',
                servicefile,
                context={'INSTANCE': self.instance, 'USERNAME': self.username},
                use_sudo=True,
                backup=False
            )
        sudo("systemctl daemon-reload")
        sudo("systemctl enable {}".format(self.username))

    def stop_odoo(self):
        sudo("systemctl stop {}".format(self.username))

    def restart_odoo(self):
        sudo(
            "systemctl restart {0} && "
            "systemctl status -l --no-pager {0} -l"
            .format(self.username)
        )
        print 'Waiting for Odoo to start....'
        time.sleep(5)
        print 'Checking port....'
        with settings(abort_exception=FabricException):
            try:
                sudo("nc -z localhost {PORT}".format(PORT=self.port))
            except FabricException:
                sudo("tail {}".format(self.logfile))
                print 'ERROR: Odoo not running! Logfile printed'
                sys.exit(1)

    def encrypt_https_certificate(self):
        with settings(abort_exception=FabricException):
            try:
                sudo(
                   "certbot certonly -d {URL} -m info@sunflowerweb.nl -n --agree-tos --nginx"
                   .format(URL=self.url)
                )
            except FabricException:
                #sudo("systemctl status -l --no-pager {} -l".format(self.nginx_file_name))
                sudo("systemctl restart nginx")
                print 'ERROR: NGINX problem. Logfile printed.'
                sys.exit(1)

    def create_local_cfg(self):
        local_cfg_file = '{}/buildout/local.cfg'.format(self.home)
        # TODO: separate auto.cfg and manual.cfg
        upload_template(
            'templates/local.cfg',
            local_cfg_file,
            context={
                'USERNAME': self.username,
                'DBUSER': self.dbuser,
                'INSTANCE': self.instance,
                'PASSWORD': self.password,
                'PORT': self.port,
                'LONG_PORT': self.port + 1,
            },
            use_sudo=True,
            backup=False
        )
        sudo('chown {1}:{1} {0}'.format(local_cfg_file, self.username))

    def get_port(self):
        # TODO: replace this with unix socket
        port_lines = sudo(
            "lsof -P -i -n -sTCP:LISTEN",
        )
        open_ports = []
        for port_details in port_lines.splitlines():
            p = port_details.split(':')
            port = p[-1][:-9]
            if port.isdigit():
                open_ports.append(int(port))

        return max(open_ports) + 2

    def configure_nginx(self): 
        nginx_file = "/etc/nginx/sites-available/" + self.nginx_file_name
        url = self.url

        upload_template(
            'templates/nginx.conf',
            nginx_file,
            context={
                'SERVERNAME': url,
                'NGINXFILE': self.nginx_file_name,
                'PORT': self.port,
                'LONGPORT': self.port + 1,
            },
            use_sudo=True,
            backup=False,
        )
        sudo("nginx_ensite {} && systemctl restart nginx".format(self.nginx_file_name))

    def upgrade_odoo(self):
        upgrade_script = "{}/buildout/bin/upgrade_odoo".format(self.home)
        with settings(abort_exception=FabricException):
            try:
                os.system("ssh {} 'cd $HOME/buildout && bin/upgrade_odoo'".format(self.username))
            except FabricException:
                sudo("tail {}/buildout/upgrade.log".format(self.home))
                print 'ERROR: Upgrade failed! Logfile printed'
                sys.exit(1)

    def create_config_file(self):
        upload_template(
            'templates/odooconfig.json',
            self.odooconfigfile,
            context={
                'USERNAME': self.username,
                'DBUSER': self.dbuser,
                'PASSWORD': self.password,
                'PORT': self.port,
            },
            use_sudo=True,
            backup=False
        )
        sudo('chown {1}:{1} {0}'.format(self.odooconfigfile, self.username))

    def add_sudo(self):
        """ Modify visudo """
        sudoers_file = "/etc/sudoers.d/{}".format(self.username)
        upload_template(
            'templates/sudoers',
            sudoers_file,
            context={
                'USERNAME': self.username,
            },
            backup=False,
            use_sudo=True
        )
        sudo("chmod 440 {}".format(sudoers_file))
        sudo("chown root:root {}".format(sudoers_file))

    def add_restart(self):
        #Add the restart script
        restart_script = "/home/{}/buildout/restart".format(self.username)
        upload_template(
            'templates/restart',
            restart_script,
            context={'USERNAME': self.username},
            use_sudo=True,
            backup=False
        )
        sudo('chown {1}:{1} {0}'.format(restart_script, self.username))
        sudo("chmod u+x {}".format(restart_script))

        stop_script = "/home/{}/buildout/stop".format(self.username)
        upload_template(
            'templates/stop',
            stop_script,
            context={'USERNAME': self.username},
            use_sudo=True,
            backup=False
        )
        sudo('chown {1}:{1} {0}'.format(stop_script, self.username))
        sudo("chmod u+x {}".format(stop_script))

    def after_installation(self):
        pass
        #sudo(
        #    "psql postgres -tAc \"ALTER USER {} NOCREATEDB\"".format(self.dbuser),
        #    user='postgres'
        #)

    def send_config_to_mail1(self, url=False, version=False, email=False):
 	msg = """\
		memee\
		MMDMDMDMDM\
		{}-{}-{}
		""".format(self.instance, url, version)
	#sudo ('echo blabla | mail nza.terrence@gmail.com')
        script = "echo {} | mail nza.terrence@gmail.com".format(msg)
	p = subprocess.Popen(script, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)        
	#p = Popen([], stdin=PIPE)
        p.communicate()[0]
	 #sudo('echo {} | mail {}'.format(msg, email))
	print  "mail sent"

def install_odoo(instance=False, url=False, version=False, email=False):
    #fab install_odoo:instance=testv2,url=testurl
    if not instance or not url or not version or not email:
        print """
        Run with arguments eg:
        fab install_odoo:instance=testv2,url=testurl
        Some arguments are missing:
        1. Required Arguments are:
            instance=INSTANCE_NAME
            version=INSTANCE_VERSION eg. 8, 9, 10
            email=INSTANCE_SETTINGS_EMAIL
        2. Optional Arguments are:
            url=INSTANCE_URL eg. test.1systeem.nl
        """
    else:
        odoo = OdooInstance(instance=instance)
        odoo.check_exist()
        odoo.configure_unix_user()
        odoo.install_odoo(url=url, version=version, email=email)
        odoo.send_config_to_mail(url=url, version=version, email=email) 
        print('Yay, we are done, visit your odoo instance at: \n https://{}'.format(odoo.url))
	print "its done"


def prepserver():
    sudo('apt-get update')
    sudo('apt-get install software-properties-common')
    sudo('add-apt-repository ppa:certbot/certbot')
    sudo('apt-get update')
    sudo('apt-get install python-certbot-nginx')
    sudo('apt-get install git')
    sudo('apt-get install postgresql')
    sudo('apt-get install nginx')
    sudo('openssl dhparam -dsaparam -out /etc/ssl/certs/dhparam.pem 4096')


def reconfigure(instance=False):
    if not instance:
        print """
        Run with arguments eg:
        fab reconfigure:instance=testv2
        """
    odoo = OdooInstance(instance=instance)
    odoo.configure_unix_user()


def buildout(instance=False):
    if not instance:
        print """
        Run with arguments eg:
        fab reconfigure:instance=testv2
        """
    odoo = OdooInstance(instance=instance)
    odoo.reload_config_from_remote()
    odoo.rebuild_odoo()


def backup():
    # can use this to do for each buildout 
    # require('buildouts', provided_by=[irodion])
    
    for host in env.hosts:
        date = time.strftime('%Y%m%d%H%M%S')
        fname = '/tmp/{host}-backup-{date}.xz'.format(**{
            'host': host,
            'date': date,
        })

        output = sudo(
            "psql -P pager -t -A -c 'SELECT datname FROM pg_database'",
            user='postgres'
        )
        for database in output.splitlines():
            fname = '/tmp/{host}-{database}-backup-{date}.xz'.format(**{
                'host': host,
                'database': database,
                'date': date,
            })
            if exists(fname):
                run('rm "{0}"'.format(fname))

            #pg_dump $db |gzip -f > /tmp/pg_$db.sql.gz
            # sudo su - postgres
            sudo('cd; pg_dump {database} | xz > {fname}'.format(**{
                'database': database, 
                'fname': fname,
            }), user='postgres')

        #if exists(fname):
        #    run('rm "{0}"'.format(fname))
        #
        #sudo('cd; pg_dumpall | xz > {0}'.format(fname), user='postgres')
        #
        get(fname, os.path.basename(fname))
        sudo('rm "{0}"'.format(fname), user='postgres')

    # def backup():

    # sudo to backup user
    # catch all the legacy backups
    # copy them here
    # remove them there.
    # mail someone if there is some missing
    # that doesnt have the required date or name

    # sudo to each odoo
    # install oca backup module if its not there yet
    # do the backup from a buildout script
    # download it


