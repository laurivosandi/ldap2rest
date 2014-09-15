# encoding: utf-8

import falcon
import ldap
import os
import random
import re
import settings
import string
from ldap import modlist
from datetime import datetime
from forms import validate, required
from util import serialize, domain2dn, dn2domain, generate_password
from resources.auth import auth

assert hasattr(settings, "BASE_DOMAIN")
assert hasattr(settings, "HOME")
assert hasattr(settings, "ADMIN_NAME")
assert hasattr(settings, "ADMIN_EMAIL")

class UserListResource:
    def __init__(self, conn, mailer=None):
        self.conn = conn
        self.mailer = mailer
        
    @serialize
    def on_get(self, req, resp, domain=settings.BASE_DOMAIN):
        user_fields = "mobile", "gender", "dateOfBirth", "cn", "givenName", \
            "sn", "uid", "uidNumber", "gidNumber", "homeDirectory", \
            "modifyTimestamp", settings.LDAP_USER_ATTRIBUTE_ID,
        args = domain2dn(domain), ldap.SCOPE_SUBTREE, "objectClass=posixAccount", user_fields
        users = dict()
        for dn, attributes in self.conn.search_s(*args):
            m = re.match("cn=(.+?),ou=people,(.+)$", dn)
            cn, dcs = m.groups()
            users[dn] = dict(
                id = attributes.get(settings.LDAP_USER_ATTRIBUTE_ID, [None]).pop(),
                recovery_email = settings.LDAP_USER_ATTRIBUTE_RECOVERY_EMAIL,
                domain = dn2domain(dcs),
                born = attributes.get("dateOfBirth", [None]).pop(),
                username = attributes.get("uid").pop(),
                uid = int(attributes.get("uidNumber").pop()),
                gid = int(attributes.get("gidNumber").pop()),
                home = attributes.get("homeDirectory").pop(),
                givenName = attributes.get("gn", [None]).pop(),
                sn = attributes.get("sn", [None]).pop(),
                cn = attributes.get("cn").pop(),
                modified = datetime.strptime(attributes.get("modifyTimestamp").pop(), "%Y%m%d%H%M%SZ"))
        return users

    @auth
    @serialize
    @required("cn")
    @validate("group",     r"[a-z]{1,32}$", required=False)
    @validate("id",        r"[3-6][0-9][0-9][01][0-9][0-3][0-9][0-9][0-9][0-9][0-9]$", required=False)
    @validate("username",  r"[a-z][a-z0-9]{1,31}$", required=True)
    def on_post(self, req, resp, domain=settings.BASE_DOMAIN):
        fullname = req.get_param("cn")
        username = req.get_param("username")
        print type(fullname), fullname, type(username), username
        home = settings.HOME(username, domain)
        first_name, last_name  = fullname.rsplit(" ", 1)
        initial_password = generate_password(8)
        
        dn_user = "cn=%s,ou=people,%s" % (fullname, domain2dn(domain))
        dn_group = "cn=%s,ou=groups,%s" % (username, domain2dn(domain))
        
        # Make sure we're not getting hacked
        RESERVED_GROUPS = set(["root", "audio", "video", "wheel", "sudo", \
            "admin", "daemon", "bin", "lp", "pulse", "lightdm", "dnsmasq", \
            "nobody", "nogroup", "shadow", "kvm", "tape", "floppy", "cdrom", \
            "nslcd", "proxy", "man", "news", "tty", "adm", "disk"])
        
        if username in RESERVED_GROUPS: # TODO: Use better HTTP status code
            raise falcon.HTTPConflict("Error", "Username is reserved")

        # Search for already existing username
        args = domain2dn(settings.BASE_DOMAIN), ldap.SCOPE_SUBTREE, "(&(objectClass=posixAccount)(uid=%s))" % username, []
        for dn, attributes in self.conn.search_s(*args):
            raise falcon.HTTPConflict("Error", "Username already exists")
            
        # Automatically assign UID/GID for the user
        UID_MIN = 2000
        UID_MAX = 9000
        args = domain2dn(settings.BASE_DOMAIN), ldap.SCOPE_SUBTREE, "objectClass=posixAccount", ["uidNumber"]
        uids = set()
        for dn, attributes in self.conn.search_s(*args):
            uid = int(attributes["uidNumber"].pop())
            if uid < UID_MIN: continue
            if uid > UID_MAX: continue
            if uid in uids:
                print "Overlapping UID-s for:", dn
            uids.add(uid)
        if uids:
            uid = max(uids) + 1
        else:
            uid = UID_MIN
        if uid > UID_MAX: # TODO: Use better HTTP status code
            raise falcon.HTTPConflict("Error", "Out of UID-s!")
            
        # Extract domain full name
        args = domain2dn(domain), ldap.SCOPE_BASE, "objectClass=domain", ["description"]
        for _, attributes in self.conn.search_s(*args):
            domain_description = attributes.get("description", [domain]).pop().decode("utf-8")

        # Compose list of recipients for the e-mail
        if self.mailer:
            # Add ME!
            recipients = [settings.ADMIN_EMAIL]
            local_helpdesk = None
            
            # Add all local helldesk guys
            for admin_username, subdomain in settings.ADMINS.items():
                if subdomain.endswith("." + domain) or subdomain == domain:
                    args = domain2dn(domain), ldap.SCOPE_SUBTREE, "(&(objectClass=posixAccount)(uid=%s))" % admin_username, [settings.LDAP_USER_ATTRIBUTE_RECOVERY_EMAIL, "cn"]
                    for _, attributes in self.conn.search_s(*args):
                        admin_email = attributes.get(settings.LDAP_USER_ATTRIBUTE_RECOVERY_EMAIL, [""]).pop()
                        if "@" in admin_email:
                            admin_email = admin_email.replace("@", "+helpdesk@")
                            if domain not in admin_email:
                                admin_email = admin_email.replace("@", "+%s@" % domain)
                            recipients.append(admin_email)
                            local_helpdesk = {"email": admin_email, "name": attributes.get("cn").pop().decode("utf-8")}

            # Add the related user himself
            if req.get_param("email"):
                recipients.append(req.get_param("email"))
                            

        ldif_user = modlist.addModlist({
            settings.LDAP_USER_ATTRIBUTE_ID: req.get_param("id") or [],
            settings.LDAP_USER_ATTRIBUTE_RECOVERY_EMAIL: req.get_param("email"),
            "uid": username,
            "uidNumber": str(uid),
            "gidNumber": str(uid),
            "sn": last_name,
            "givenName": first_name,
            "preferredLanguage": "en_US",
            "homeDirectory": home,
            "loginShell": "/bin/bash",
            "objectclass": ["top", "person", "organizationalPerson", "inetOrgPerson", "posixAccount", "shadowAccount", "gosaAccount"]
        })

        ldif_group = modlist.addModlist(dict(
            objectClass = ['top', 'posixGroup'],
            memberUid = [username],
            gidNumber = str(uid),
            cn = username,
            description = "Group of user %s" % fullname))
            
        ldif_ou_people = modlist.addModlist(dict(
            objectClass = ["organizationalUnit"],
            ou = "people"))
            
        ldif_ou_groups = modlist.addModlist(dict( 
            objectClass = ["organizationalUnit"],
            ou = "groups"))

        try:
            print "Adding ou=people if neccessary"
            self.conn.add_s("ou=people," + domain2dn(domain), ldif_ou_people)
        except ldap.ALREADY_EXISTS:
            pass
            
        try:
            print "Adding ou=groups if neccessary"
            self.conn.add_s("ou=groups," + domain2dn(domain), ldif_ou_groups)
        except ldap.ALREADY_EXISTS:
            pass

        try:
            print "Adding user:", dn_user, ldif_user
            self.conn.add_s(dn_user, ldif_user)
        except ldap.ALREADY_EXISTS:
            raise falcon.HTTPConflict("Error", "User with such full name already exists")

        # Set initial password
        self.conn.passwd_s(dn_user, None, initial_password)

        try:
            print "Adding group:", dn_group, ldif_group
            self.conn.add_s(dn_group, ldif_group)
        except ldap.ALREADY_EXISTS:
            raise falcon.HTTPConflict("Error", "Group corresponding to the username already exists")

        if req.get_param("group"):
            ldif = (ldap.MOD_ADD, 'memberUid', username),
            self.conn.modify_s("cn=%s,ou=groups,%s" % (req.get_param("group"), domain2dn(settings.BASE_DOMAIN)), ldif)

        if self.mailer:
            self.mailer.enqueue(
                settings.ADMIN_EMAIL,
                recipients,
                u"%s jaoks loodi konto %s" % (fullname, username),
                "email-user-added",
                domain={"description": domain_description},
                username = username,
                password = initial_password,
                local_helpdesk = local_helpdesk,
                server_helpdesk={"email": settings.ADMIN_EMAIL, "name": settings.ADMIN_NAME}
            )
        return dict(
            id = req.get_param("id"),
            domain = domain,
            cn = fullname,
            username = username,
            uid = uid,
            gid = uid,
            first_name = first_name,
            last_name = last_name,
            home = home)
