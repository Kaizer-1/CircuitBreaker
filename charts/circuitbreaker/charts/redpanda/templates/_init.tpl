{{/*
The init Job's pod template. A Job's template cannot change once created, so the Job is named after a hash of this
template: any change (script, image, settings) makes a new Job, and Helm removes the old one.
*/}}
{{- define "redpanda.initPod" }}
  template:
    metadata:
      labels: {app.kubernetes.io/name: redpanda-init, app.kubernetes.io/part-of: circuitbreaker}
      annotations:
        checksum/script: {{ .Files.Get "files/create-topics.sh" | sha256sum }}
    spec:
      enableServiceLinks: false
      restartPolicy: OnFailure
      containers:
        - name: init
          image: {{ .Values.image }}
          command:
            - bash
            - -c
            # Wait for the broker (Compose's depends_on: service_healthy), then create the topics.
            - until rpk -X brokers=redpanda:9092 cluster info >/dev/null 2>&1; do sleep 2; done;
              bash /init/create-topics.sh
          env:
            - {name: REDPANDA_BROKERS, value: "redpanda:9092"}
          volumeMounts:
            - {name: init, mountPath: /init}
          resources:
            requests: {cpu: 50m, memory: 64Mi}
            limits: {cpu: 500m, memory: 128Mi}
      volumes:
        - name: init
          configMap: {name: redpanda-init}
{{- end }}
