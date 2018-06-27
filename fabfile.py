# Fabfile to:
#    - Download backups of all odoos

import io
import json
import os
import sys
import time

from os.path import expanduser

from fabric.context_managers import *
from fabric.contrib.files import exists, upload_template
from fabric.api import *


class FabricException(Exception):
    pass


class OdooInstance:
    
    def __init__(self, instance=False, url=False, version=False, email=False):
        self.branch = '{0}.0-custom-standard'.format(version)
        self.cfg = 'odoo{0}-standard.cfg'.format(version)
        self.url = url or instance + '.1systeem.nl'
        self.password = self.get_password()
        self.port = self.get_port()
        self.instance = instance
        self.username = "odoo-" + instance
        self.home = "/home/{}".format(self.username)
        self.logfile = "{}/{}.log".format(self.home, self.username)
        self.dbuser = "odoo" + instance.replace('-','')
        self.nginx_file_name = "odoo_" + self.instance
        self.email = email
        print self.branch, self.cfg

        if not self.unix_user_exists():
            print "Setting up Unix User..."
            self.setup_unix_user()
        self.setup_postgres_user()
        self.add_host_to_ssh_config()
        self.ssh_git_clone()
        self.create_local_cfg()
        self.add_restart()
        self.add_sudo()
        self.run_buildout()
        self.add_and_start_odoo_service()
        self.encrypt_https_certificate()
        self.configure_nginx()
        self.create_config_file()
        self.after_installation()
        self.send_config_to_mail()
    
    def get_password(self):
        return sudo("pwgen | awk '{print $1;}'")

    def setup_unix_user(self):
        sudo(
            "PASSWORD={PASSWORD}; \
            echo $PASSWORD; \
            INSTANCE={INSTANCE}; \
            echo $INSTANCE; \
            USERNAME=odoo-$INSTANCE; \
            echo $USERNAME; \
            adduser $USERNAME --disabled-password --gecos GECOS;\
            echo $USERNAME:$PASSWORD | chpasswd;\
            mkdir -p /home/$USERNAME/.ssh;\
            cp /home/odoo-sunflower/.ssh/authorized_keys /home/$USERNAME/.ssh/authorized_keys;\
            cp /home/odoo-sunflower/.vimrc /home/$USERNAME/.vimrc;\
            chmod 700 /home/$USERNAME/.ssh;\
            chmod 600 /home/$USERNAME/.ssh/authorized_keys;\
            chown -R $USERNAME:$USERNAME /home/$USERNAME/.ssh /home/$USERNAME/.vimrc"
            .format(
                INSTANCE=self.instance,
                PASSWORD=self.password
            )
        )
        print('Unix User Setup Successful...')

    def setup_postgres_user(self):
        if not self.postgres_user_exists():
            sudo("createuser {}".format(self.dbuser), user='postgres')
        sudo(
            "psql postgres -tAc \"ALTER USER {DBUSER} WITH PASSWORD '{PASSWORD}'\" && "
            "psql postgres -tAc \"ALTER USER {DBUSER} CREATEDB\""
            .format(
                DBUSER=self.dbuser,
                PASSWORD=self.password,
            ),
            user='postgres'
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
            warn_only=True
        ):
            check_user = sudo(
                "psql postgres -tAc \"SELECT 1 FROM pg_roles WHERE rolname='{DBUSER}'\"".format(
                    DBUSER=self.dbuser,
                ),
                user='postgres'
            )
            return check_user
           
    def add_host_to_ssh_config(self):
        home = expanduser("~")
        config_file = "{}/.ssh/config".format(home)

        # CHECK IF HOST EXISTS IN ./ssh/config
        host_exists = False
        searchfile = open(config_file, "r")
        for line in searchfile:
            if self.username in line: 
                host_exists = self.username
        searchfile.close()

        #ADD HOST TO ./ssh/config
        if not host_exists:
            with open(config_file, "a") as config:
                config.write("""\nHost {USERNAME}\n\
        ForwardAgent yes\n\
        HostName applejuice.sunflowerweb.nl\n\
        User {USERNAME}\n""".format(**{
                    'USERNAME': self.username,
                }),)

    def ssh_git_clone(self):
        known_hosts = sudo("find /home/{}/.ssh -name known_hosts".format(self.username))
        buildout = sudo("find /home/{} -type d -name buildout".format(self.username))
        if not known_hosts:
            print "Known Hosts does not exist, adding file known_hosts..."
            os.system("ssh {USERNAME} 'touch /home/{USERNAME}/.ssh/known_hosts'".format(
                USERNAME=self.username
            ))
            os.system("ssh {USERNAME} 'ssh-keygen -F github.com || ssh-keyscan github.com >> /home/{USERNAME}/.ssh/known_hosts'".format(USERNAME=self.username))
        if not buildout:
            print "Buildout does not exist, cloning into home dir...", self.username
            os.system("ssh {USERNAME} 'git clone git@github.com:sunflowerit/custom-installations.git --branch {BRANCH} --single-branch buildout'".format(USERNAME=self.username, BRANCH=self.branch))

    def run_buildout(self):
        os.system("ssh {} 'python buildout/bootstrap.py -c buildout/local.cfg'".format(self.username))
        os.system("ssh {} 'python buildout/bin/buildout -c buildout/local.cfg'".format(self.username))
        os.system("ssh {} 'python buildout/bin/buildout -c buildout/local.cfg'".format(self.username))

    def add_and_start_odoo_service(self):
        service = sudo("find /lib/systemd/system/ -name {0}.service".format(self.username))
        if not service:
            servicefile = '/lib/systemd/system/{}.service'.format(self.username)
            upload_template(
                'templates/odoo.service',
                servicefile,
                context={'INSTANCE': self.instance, 'USERNAME': self.username},
                use_sudo=True
            )
        sudo(
            "systemctl daemon-reload && "
            "systemctl restart {USERNAME} && "
            "systemctl status -l --no-pager {USERNAME} -l"
            .format(USERNAME=self.username)
        )
        print 'Waiting for Odoo to start....'
        time.sleep(5)
        print 'Checking port....'
        with settings(abort_exception=FabricException):
            try:
                sudo("nc -z localhost {PORT}".format(PORT=self.port))
            except FabricException:
                print 'Odoo not running!'
                print 'Logfile:'
                sudo("cat {LOGFILE}".format(LOGFILE=self.logfile))
                print 'ERROR: Odoo not running! Logfile printed'
                sys.exit(1)

    def encrypt_https_certificate(self):
        with settings(abort_exception=FabricException):
            try:
                sudo(
                   "systemctl stop nginx && "
                   "certbot certonly -d {URL} -m info@sunflowerweb.nl -n --agree-tos --standalone && "
                   "systemctl start nginx"
                   .format(URL=self.url)
                )
            except FabricException:
                sudo("systemctl status -l --no-pager {} -l".format(self.nginx_file_name))
                sudo("nginx_dissite {} && systemctl restart nginx".format(self.nginx_file_name))
                print 'ERROR: NGINX problem. Logfile printed.'
                sys.exit(1)

    def create_local_cfg(self):
        local_cfg_file = '/home/{}/buildout/local.cfg'.format(self.username)
        # TODO: separate auto.cfg and manual.cfg
        upload_template(
            'templates/local.cfg',
            local_cfg_file,
            context={
                'CFG': self.cfg,
                'USERNAME': self.username,
                'DBUSER': self.dbuser,
                'INSTANCE': self.instance,
                'PASSWORD': self.password,
                'PORT': self.port,
                'LONG_PORT': self.port + 1,
            },
            use_sudo=True
        )
        sudo('chown {USERNAME}:{USERNAME} {CFG}'.format(
            CFG=local_cfg_file,
            USERNAME=self.username,
        ))

    def get_port(self):
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
        url = self.instance + ".1systeem.nl" 

        upload_template(
            'templates/nginx.conf',
            nginx_file,
            context={
                'SERVERNAME': url,
                'NGINXFILE': self.nginx_file_name,
                'PORT': self.port,
                'LONGPORT': self.port + 1,
            },
            use_sudo=True
        )
        sudo("nginx_ensite {} && systemctl restart nginx".format(self.nginx_file_name))
        os.system("ssh {} 'buildout/bin/upgrade_odoo'".format(self.username))
        os.system("ssh {USERNAME} 'service {USERNAME} restart'".format(USERNAME=self.username))

    def create_config_file(self):
        odooconfigfile = "/home/{}/odooconfig.json".format(self.username)
        upload_template(
            'templates/odooconfig.json',
            odooconfigfile,
            context={
                'USERNAME': self.username,
                'DBUSER': self.dbuser,
                'PASSWORD': self.password,
            },
            use_sudo=True
        )

    def add_sudo(self):
        #visudo
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
        stop_script = "/home/{}/buildout/stop".format(self.username)
        upload_template(
            'templates/restart',
            restart_script,
            context={'USERNAME': self.username},
            use_sudo=True,
            backup=False
        )
        upload_template(
            'templates/stop',
            stop_script,
            context={'USERNAME': self.username},
            use_sudo=True,
            backup=False
        )
        sudo('chown {USERNAME}:{USERNAME} {SCRIPT}'.format(
            SCRIPT=restart_script,
            USERNAME=self.username,
        ))
        sudo('chown {USERNAME}:{USERNAME} {SCRIPT}'.format(
            SCRIPT=stop_script,
            USERNAME=self.username,
        ))
        sudo("chmod u+x {}".format(restart_script))
        sudo("chmod u+x {}".format(stop_script))

    def after_installation(self):
        sudo(
            "psql postgres -tAc \"ALTER USER {DBUSER} NOCREATEDB\""
            .format(
                DBUSER=self.dbuser,
                PASSWORD=self.password,
            ),
            user='postgres'
        )

    def send_config_to_mail(self):
        pass
        #TODO


def install_odoo(instance=False, url=False, version=False, email=False):
    #fab install_odoo:instance=testv2,url=testurl
    if not instance or not version or not email:
        print "\n\
        Run fab with arguments eg:\n\n\
        fab install_odoo:instance=testv2,url=testurl \n\n\
        Some arguments are missing: \n\n\
        1. Required Arguments are:\n\n\
            instance=INSTANCE_NAME\n\
            version=INSTANCE_VERSION eg. 8, 9, 10 \n\
            email=INSTANCE_SETTINGS_EMAIL\n\n\
        2. Optional Arguments are:\n\n\
            url=INSTANCE_URL eg. test.1systeem.nl\n\
        "
    else:
        odoo = OdooInstance(instance=instance, url=url, version=version, email=email)
        print('Yay, we are done, visit your odoo instance at: \n https://{}'.format(odoo.url))


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


