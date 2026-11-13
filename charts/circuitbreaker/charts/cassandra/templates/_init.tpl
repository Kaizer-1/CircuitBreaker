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
          command:
            - bash
            - -c
            # Wait for CQL (Compose's depends_on: service_healthy), then create keyspaces and roles.
            - until cqlsh cassandra -u "$CASSANDRA_ADMIN_USER" -p "$CASSANDRA_ADMIN_PASSWORD"
              -e 'SELECT release_version FROM system.local' >/dev/null 2>&1; do sleep 5; done;
              bash /init/init-keyspaces.sh
          env:
            - {name: CASSANDRA_HOST, value: cassandra}
            - {name: CASSANDRA_ADMIN_USER, value: cassandra}
            - name: CASSANDRA_ADMIN_PASSWORD
              valueFrom: {secretKeyRef: {name: {{ .Values.global.secrets.cassandraAdmin }}, key: password}}
            {{- range $ks, $secret := .Values.roles }}
            - name: PASSWORD_{{ $ks }}
              valueFrom: {secretKeyRef: {name: {{ $secret }}, key: password}}
            {{- end }}
          volumeMounts:
            - {name: scripts, mountPath: /init}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: 1000m, memory: 256Mi}
      volumes:
        - name: scripts
          configMap: {name: cassandra-scripts}
{{- end }}
