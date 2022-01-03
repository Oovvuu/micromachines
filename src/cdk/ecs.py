"""Standardised patterns for working with ECS."""

import math

from attr import attrib
from attr import attrs
from aws_cdk.aws_ecs import HealthCheck as EcsHealthCheck
from aws_cdk.aws_elasticloadbalancingv2 import HealthCheck as ElbHealthCheck
from aws_cdk.aws_elasticloadbalancingv2 import Protocol as ElbProtocol
from aws_cdk.core import Duration


@attrs(kw_only=True, init=True)
class HealthCheckConfig:
    """Correctly models health-check-related configuration for an
    autoscaled ECS Service with associated ALB, that is deployed through
    CloudFormation.

    If...
        * The ECS service is "healthy" before a deployment starts; and
        * The deployment uses a new task definition; and
        * The new task definition breaks, such that the ECS health check fails

    ...then ECS+CloudFormation will (eventually) detect that the deployment is
    broken and roll it back, without negatively affecting the ECS Service.

    However, in order for this to work there are a lot of undocumented (or
    inconsistently documented) settings in a number of places that need to be
    correct, both individually and together. This class encapsulates and
    documents that correct configuration.

    tl;dr: Configure the ECS health check to fail fast, and the ALB health
    check to fail very slowly.
    """

    #: Maximum number of seconds it takes for the container to startup
    #: (from "Started" until ready to serve network traffic)
    max_container_startup: int = attrib(default=30)

    #: Maximum number of seconds to wait for a health check request to
    #: complete before deeming it to be a failure
    timeout: int = attrib(default=5)

    #: Number of seconds to wait between health check requests on the container
    check_interval_ecs: int = attrib(default=10)

    #: Number of seconds to wait between health check requests from ALB
    check_interval_alb: int = attrib(default=30)

    #: Number of consecutive failed health check requests on the container before the
    #: container is marked as unhealthy.
    num_checks_ecs: int = attrib(default=2)

    #: Number of consecutive failed health check requests from ALB before the
    #: container is marked as unhealthy.
    num_checks_alb: int = attrib(default=5)

    #: Absolute path for the health check endpoint
    endpoint_path: str = attrib(default="/")

    #: TCP port that the container listens on
    container_port: int = attrib(default=8000)

    @property
    def min_time_to_unhealthy_alb(self) -> int:
        """The shortest time in which ALB could detect that a task is unhealthy, at startup."""
        # The actual minimum interval at which ALB performs a health check request.
        # The ALB interval is considered an approximate guideline, so we
        # reduce this to a conservatively short value
        #
        # Note that the check interval is not affected by a long or timed-out
        # request - see https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html
        min_check_interval = math.floor(
            self.check_interval_alb - max(2, self.check_interval_alb * 0.2)
        )

        return (self.num_checks_alb - 1) * min_check_interval

    @property
    def max_time_to_unhealthy_ecs(self) -> int:
        """The longest time ECS could take to detect that a task is unhealthy, at startup."""
        # The actual maximum interval at which ECS performs a health check command.
        #
        # Note that the interval for a docker health-check request starts
        # *after* the previous request completes - see
        # https://docs.docker.com/engine/reference/builder/#healthcheck
        #
        # Also note that the actual timeout for our ECS command is 1 second more
        # than the timeout for the HTTP request
        actual_check_interval = (self.timeout + 1) + self.check_interval_ecs

        # ECS/Docker doesn't start counting failed health check requests until
        # after the startup[ period.
        max_time = self.max_container_startup

        # The health check request cycle is independent of the container
        # startup process, so it could take up to one checking interval
        # before the first health check request is made.
        max_time += actual_check_interval

        # ECS then needs several consecutive requests before the container
        # state is marked as unhealthy
        max_time += (self.num_checks_ecs - 1) * actual_check_interval

        return max_time

    def get_alb_config(
        self, protocol: ElbProtocol = ElbProtocol.HTTP, port: int = None
    ) -> ElbHealthCheck:
        """Get the health check configuration for the ALB Target Group.

        Parameters:
            port: Use a different port for the health check. By default
                ALB uses the traffic port
            protocol: Specify the protocol to use for the health check. By
                default, this is non-SSL HTTP.
        """
        # An ALB load balancer uses it's health check to determine which tasks
        # in the ECS service should receive traffic. Additionally, it will
        # notify the ECS system to stop any tasks that it determines are
        # unhealthy. The ALB health check configuration is mostly
        # independent of the ECS health check configuration.
        #
        # The same health check configuration is used to determine when a
        # newly started task is ready to receive traffic, and also to
        # determine if an ongoing task has become broken.
        #
        # There is a problem with the whole-system configuration, in that
        # if the ALB stops a newly started task that it thinks is unhealthy,
        # then ECS doesn't realise that the service is unhealthy, and the
        # deployment will incorrectly succeed.
        #
        # The solution is to tweak the health check configurations so that
        # the ECS health check quickly detects and stops an unhealthy task
        # (either at startup or ongoing), and the ALB health check never
        # stops a task at startup.
        #
        # Therefore we create an ALB health check configuration that is quite
        # slow. This means that some genuine problems don't get automatically
        # resolved in a timely manner, but TBH the nature of this category of
        # problem (external to the container itself) is unusual.

        if self.timeout >= self.check_interval_alb:
            raise ValueError(
                "Healthcheck timeout is longer than the ALB repeat interval"
            )
        if (self.min_time_to_unhealthy_alb + 30) < self.max_time_to_unhealthy_ecs:
            raise ValueError(
                "Healthcheck timing means that the ALB might stop an unhealthy "
                "ECS task before ECS does, which is undesirable."
            )

        return ElbHealthCheck(
            # Unhealthy ECS tasks always get stopped and never get to recover,
            # therefore there is no point setting this.
            # healthy_threshold_count=5,
            interval=Duration.seconds(self.check_interval_alb),
            path=self.endpoint_path,
            port=port,
            protocol=protocol,
            timeout=Duration.seconds(self.timeout),
            unhealthy_threshold_count=self.num_checks_alb,
        )

    def get_ecs_config(self, command: str = None) -> EcsHealthCheck:
        """Get the health check configuration for the ECS container definition for a webserver container.

        Parameters:
            command: By default we configure the ECS health check to do a HTTP
                GET on the health check endpoint. This parameter specifies an
                alternate command.
        """
        # ECS will replace any task that is part of a service, and
        # is deemed to be unhealthy (because any essential container in that
        # task is unhealthy). Therefore we need at least 1 container-level
        # healthcheck in the task definition.

        # If you don't specify a health check in the container definition in
        # the task definition, then ECS won't know about it (even if it is
        # baked into the image after being specified in the Dockerfile).
        # Therefore we always define a health check in the container definition
        #
        # See https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html#container_definition_healthcheck

        if self.timeout >= self.check_interval_ecs:
            raise ValueError(
                "Healthcheck timeout is longer than the ECS repeat interval"
            )

        if not command:
            # Use `wget` rather than `curl`, since it is present in more
            # vanilla OS images (eg. Alpine)
            command = f'wget -T {self.timeout} -O - "http://localhost:{self.container_port}{self.endpoint_path}"'

        return EcsHealthCheck(
            command=[command + " || exit 1"],
            interval=Duration.seconds(self.check_interval_ecs),
            # Docker calls it "retries", but it's actually total number of
            # attempts, not first attempt + N retries.
            # See https://docs.docker.com/engine/reference/builder/#healthcheck
            retries=self.num_checks_ecs,
            start_period=Duration.seconds(self.max_container_startup),
            # Make the Docker service's timeout be longer than the `wget`
            # timeout, so that we get output from `wget`
            timeout=Duration.seconds(self.timeout + 1),
        )

    def get_ecs_service_properties(self) -> dict:
        """Get health-check-related properties for an ECS Service."""
        # This is the period of time that an unhealthy state is not acted
        # on - an unhealthy task won't be stopped inside this period.
        # Note that failed health check requests are still counted as
        # contributing to an unhealthy state.
        #
        # Also note that the same value is used by both ECS container
        # health checks and ALB health checks - see
        # https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service_definition_parameters.html
        # This means that if ALB determines that the task is unhealthy
        # before this time finished, then ALB will stop the task instead
        # of ECS and our deployment model will break.
        #
        # Therefore we hard-code it to something small, in order to ensure
        # it has no impact.
        grace_period = 10

        if self.min_time_to_unhealthy_alb < grace_period:
            raise ValueError(
                "The ECS grace period is too long, which means that ALB might "
                "mark a container as unhealthy at the same time as ECS."
            )

        return dict(
            health_check_grace_period=Duration.seconds(grace_period),
            # The default healthy percentages are reasonable.
            #
            # It's also possible that they are ignored for Fargate deployments - see
            # https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service_definition_parameters.html
            #
            # Therefore we don't set these.
            # min_healthy_percent=50,
            # max_healthy_percent=200,
        )
