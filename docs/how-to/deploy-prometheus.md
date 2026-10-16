# Provision Prometheus and the Collector

Prometheus and the OpenTelemetry Collector are deployed through the team Ansible repository: [testpress/testpress-ansible](https://github.com/testpress/testpress-ansible).

Add the application or collector host IP to the Prometheus allow list in that deployment before sending telemetry.

This platform uses the Prometheus push method. It does not depend on a Prometheus scrape of each application instance. Configure applications to send OTLP to the Collector endpoint provided by the Ansible deployment.

After deployment, make a request through an instrumented application and use Grafana to confirm that its trace and metrics appear.
