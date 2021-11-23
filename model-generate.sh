#!/bin/sh
set -xe

package='gitlab-ce'
version=$(dpkg-query -f '${Version}\n' -W "$package")
ver=$(echo "$version" | cut -d. -f1,2 | tr -d .)
user="$USER"

model=src/tracboat/gitlab/model/model$ver.py
sudo -H -u gitlab-psql /usr/bin/env ~${user}/.conda/envs/tracboat/bin/pwiz.py -u gitlab-psql --engine=postgresql --host=/var/opt/gitlab/postgresql gitlabhq_production > $model

patch $model <<EOF
--- a/src/tracboat/gitlab/model/model144.py
+++ b/src/tracboat/gitlab/model/model144.py
@@ -9,14 +9,13 @@
 from peewee import *
 from playhouse.postgres_ext import *
 
-database = PostgresqlDatabase('gitlabhq_production', **{'host': '/var/opt/gitlab/postgresql', 'port': 5432, 'user': 'gitlab-psql'})
-
+database_proxy = Proxy()
 class UnknownField(object):
-    def __init__(self, *_, **__): pass
+    pass
 
 class BaseModel(Model):
     class Meta:
-        database = database
+        database = database_proxy
 
 class AbuseReports(BaseModel):
     cached_markdown_version = IntegerField(null=True)
EOF

chmod a+r $model
git add $model
git commit -m "add model for $version" $model
