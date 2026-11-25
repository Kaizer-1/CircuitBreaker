{{/*
The init Job's pod template. A Job's template cannot change once created, so the Job is named after a hash of this
template: any change (script, image, settings) makes a new Job, and Helm removes the old one.
*/}}
{{- define "cassandra.initPod" }}
  template:
    metadata:
      labels: {app.kubernetes.io/name: cassandra-init, app.kubernetes.io/part-of: circuitbreaker}
      annotations:
        checksum/script: {{ .Files.Get "files/init-keyspaces.sh" | sha256sum }}
    spec:
      enableServiceLinks: false
      restartPolicy: OnFailure
      containers:
        - name: init
          image: {{ .Values.image }}
          # Waits for CQL, gives a cluster that still has the image's default superuser password the Secret's (once),
          # then creates keyspaces and roles (dev/cassandra/init-keyspaces.sh).
          command: [bash, /init/init-keyspaces.sh]
          env:
            - {name: CASSANDRA_HOST, value: cassandra}
            - {name: CASSANDRA_ADMIN_USER, value: cassandra}
            # A file, not an environment variable: a mounted Secret updates in place, so the job sees a new value
            # that Argo CD syncs after it started.
            - {name: CASSANDRA_ADMIN_PASSWORD_FILE, value: /etc/cassandra-admin/password}
            - {name: ROTATE_DEFAULT_SUPERUSER, value: "true"}
            {{- range $ks, $secret := .Values.roles }}
            - name: PASSWORD_{{ $ks }}
              valueFrom: {secretKeyRef: {name: {{ $secret }}, key: password}}
            {{- end }}
          volumeMounts:
            - {name: scripts, mountPath: /init}
            - {name: admin, mountPath: /etc/cassandra-admin, readOnly: true}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: 1000m, memory: 256Mi}
      volumes:
        - name: scripts
          configMap: {name: cassandra-scripts}
        - name: admin
          secret: {secretName: {{ .Values.global.secrets.cassandraAdmin }}, items: [{key: password, path: password}]}
{{- end }}
