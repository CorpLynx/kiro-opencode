"""--ai (Bedrock), --iam and --eks. moto has no Bedrock, so those answers are
supplied by an Overlay; IAM and EKS run against moto itself."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_aws import (ACCOUNT, OTHER, REGION, AwsTestCase, Overlay, boto3, denied,  # noqa: E402
                      parse)
from topomapper.collectors import aws, aws_ai, aws_iam  # noqa: E402
from topomapper.model import Topology  # noqa: E402

try:
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    ClientError = Exception


def summary(model_id, name, provider):
    return {"modelId": model_id, "modelName": name, "providerName": provider,
            "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"], "responseStreamingSupported": True,
            "modelLifecycle": {"status": "ACTIVE"}}


def available(**over):
    answer = {"authorizationStatus": "AUTHORIZED", "entitlementAvailability": "AVAILABLE",
              "regionAvailability": "AVAILABLE", "agreementAvailability": {"status": "AVAILABLE"}}
    answer.update(over)
    return answer


MODELS = {"modelSummaries": [summary("anthropic.claude-a", "Claude A", "Anthropic"),
                             summary("amazon.titan-b", "Titan B", "Amazon"),
                             summary("meta.llama-c", "Llama C", "Meta"),
                             summary("cohere.command-d", "Command D", "Cohere"),
                             summary("amazon.nova-e", "Nova E", "Amazon")]}
ACCESS = {"anthropic.claude-a": available(agreementAvailability={"status": "NOT_AVAILABLE"}),
          "amazon.titan-b": available(),
          "meta.llama-c": available(),
          "cohere.command-d": available(),
          "amazon.nova-e": available()}


def bedrock(client, **over):
    answers = {"list_foundation_models": MODELS,
               "get_foundation_model_availability": lambda modelId: ACCESS[modelId],
               "list_custom_models": {"modelSummaries": []},
               "list_imported_models": {"modelSummaries": []},
               "list_provisioned_model_throughputs": {"provisionedModelSummaries": []}}
    answers.update(over)
    return Overlay(client, **answers)


def used(*model_ids):
    return {"Metrics": [{"Namespace": "AWS/Bedrock", "MetricName": "Invocations",
                         "Dimensions": [{"Name": "ModelId", "Value": m}]} for m in model_ids]}


def invoke_policy(*resources, action="bedrock:InvokeModel"):
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": [action], "Resource": list(resources)}]})


def foundation(model, region=REGION):
    return f"arn:aws:bedrock:{region}::foundation-model/{model}"


class AiTest(AwsTestCase):
    def collect_ai(self, usage=(), cloudwatch=None, **over):
        self.overrides[("bedrock", None)] = lambda c: bedrock(c, **over)
        self.overrides[("cloudwatch", None)] = lambda c: Overlay(
            c, list_metrics=cloudwatch or used(*usage))
        return self.collect("--region", REGION, "--ai", "--skip-network")

    def grant(self, role, document, name="bedrock-invoke"):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_role(RoleName=role, AssumeRolePolicyDocument=trust(service="ec2.amazonaws.com"))
        iam.put_role_policy(RoleName=role, PolicyName=name, PolicyDocument=document)
        return iam

    def models(self, topo):
        return {n.attrs["model_id"]: n for n in topo.by_kind("bedrock-model")}

    def test_only_models_in_use_or_granted_are_drawn(self):
        self.grant("app", invoke_policy(foundation("amazon.titan-b"),
                                        foundation("anthropic.claude-a")))
        topo = self.collect_ai(usage=["amazon.nova-e"])
        group = topo.nodes[f"aws:bedrock:{ACCOUNT}:{REGION}"]
        self.assertEqual(group.parent, f"aws:region:{ACCOUNT}:{REGION}")
        self.assertEqual((group.attrs["models_shown"], group.attrs["models_offered"]), (3, 5))
        shown = self.models(topo)
        self.assertEqual(sorted(shown), ["amazon.nova-e", "amazon.titan-b", "anthropic.claude-a"])
        self.assertEqual(shown["amazon.nova-e"].attrs["evidence"], ["in use"])
        self.assertEqual(shown["amazon.titan-b"].attrs["evidence"], ["granted"])
        self.assertEqual(shown["amazon.titan-b"].attrs["granted_to"], ["app"])
        self.assertNotIn("meta.llama-c", shown)            # offered, but nobody uses or is granted it
        self.assertEqual(topo.nodes[f"aws:region:{ACCOUNT}:{REGION}"].attrs[
            "bedrock_models_offered"], 5)

    def test_a_grant_nobody_can_use_is_marked(self):
        self.grant("app", invoke_policy(foundation("anthropic.claude-a"),
                                        foundation("amazon.titan-b")))
        shown = self.models(self.collect_ai())
        self.assertIs(shown["anthropic.claude-a"].attrs["invocable"], False)   # agreement missing
        self.assertIs(shown["amazon.titan-b"].attrs["invocable"], True)

    def test_inference_profiles_and_patterns(self):
        self.grant("app", invoke_policy(
            f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/us.amazon.nova-e",
            foundation("meta.llama-*")))
        shown = self.models(self.collect_ai(usage=["us.anthropic.claude-a"]))
        self.assertEqual(sorted(shown), ["amazon.nova-e", "anthropic.claude-a", "meta.llama-c"])

    def test_a_grant_for_a_model_the_region_lacks_is_not_drawn(self):
        self.grant("app", invoke_policy(foundation("amazon.titan-b"), foundation("fake.not-here")))
        self.assertEqual(sorted(self.models(self.collect_ai())), ["amazon.titan-b"])

    def test_other_regions_and_other_actions_do_not_count(self):
        self.grant("elsewhere", invoke_policy(foundation("amazon.titan-b", "eu-west-1")))
        self.grant("lister", invoke_policy(foundation("meta.llama-c"),
                                           action="bedrock:ListFoundationModels"))
        iam = boto3.client("iam", region_name=REGION)
        iam.put_role_policy(RoleName="lister", PolicyName="deny", PolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": [
                {"Effect": "Deny", "Action": "bedrock:InvokeModel",
                 "Resource": foundation("amazon.nova-e")}]}))
        self.assertEqual(self.models(self.collect_ai()), {})

    def test_customer_managed_policies_count(self):
        iam = self.grant("app", invoke_policy(foundation("amazon.nova-e")), name="inline")
        arn = iam.create_policy(PolicyName="shared", PolicyDocument=invoke_policy(
            foundation("meta.llama-c")))["Policy"]["Arn"]
        iam.attach_role_policy(RoleName="app", PolicyArn=arn)
        self.assertEqual(sorted(self.models(self.collect_ai())), ["amazon.nova-e", "meta.llama-c"])

    def test_granting_everything_is_recorded_not_drawn(self):
        self.grant("admin-ish", invoke_policy("*"))
        self.grant("bedrock-star", invoke_policy(foundation("*")), name="star")
        topo = self.collect_ai()
        group = topo.nodes[f"aws:bedrock:{ACCOUNT}:{REGION}"]
        self.assertEqual(group.attrs["granted_all_to"], ["admin-ish", "bedrock-star"])
        self.assertEqual(topo.by_kind("bedrock-model"), [])

    def test_nothing_in_use_or_granted_draws_no_group_but_records_the_count(self):
        self.overrides[("bedrock", None)] = lambda c: bedrock(c)
        self.overrides[("cloudwatch", None)] = lambda c: Overlay(c, list_metrics=used())
        topo = self.collect("--region", REGION, "--ai")      # with the network, the region stays
        self.assertEqual(topo.by_kind("bedrock"), [])
        region = topo.nodes[f"aws:region:{ACCOUNT}:{REGION}"]
        self.assertEqual((region.attrs["bedrock_models_shown"],
                          region.attrs["bedrock_models_offered"]), (0, 5))

    def test_skip_network_drops_regions_with_nothing_to_show(self):
        self.assertEqual(self.collect_ai().by_kind("region"), [])

    def test_custom_and_imported_models_and_throughput(self):
        topo = self.collect_ai(
            list_custom_models={"modelSummaries": [{"modelName": "tuned", "modelArn": "arn:x/1",
                                                    "baseModelName": "Titan B"}]},
            list_imported_models={"modelSummaries": [{"modelName": "mine", "modelArn": "arn:x/2"}]},
            list_provisioned_model_throughputs={"provisionedModelSummaries": [
                {"provisionedModelName": "pt", "modelUnits": 2, "status": "InService"}]})
        sources = {n.label: n.attrs["source"] for n in topo.by_kind("bedrock-model")}
        self.assertEqual(sources, {"tuned": "custom", "mine": "imported"})
        self.assertEqual(topo.by_kind("bedrock")[0].attrs["provisioned_throughput"],
                         [{"model_units": 2, "status": "InService", "name": "pt"}])

    def test_regions_without_a_feature_are_not_warned_about(self):
        topo = self.collect_ai(
            list_custom_models=denied("ListCustomModels", "UnknownOperationException"),
            list_imported_models=denied("ListImportedModels", "AccessDeniedException"),
            list_provisioned_model_throughputs=denied("ListProvisioned", "ValidationException"))
        self.assertNotIn("warnings", topo.meta)

    def test_regions_without_bedrock_are_not_asked(self):
        asked = []
        self.overrides[("bedrock", None)] = lambda c: asked.append(1) or bedrock(c)
        with mock.patch.object(aws_ai, "service_regions", return_value=frozenset({REGION})):
            topo = self.collect("--region", "eu-north-1", "--ai", "--skip-network")
        self.assertEqual(asked, [])
        self.assertEqual(topo.by_kind("bedrock"), [])

    def test_denied_listing_skips_bedrock_in_the_region_once(self):
        topo = self.collect_ai(list_foundation_models=denied("ListFoundationModels"))
        self.assertEqual(topo.by_kind("bedrock"), [])
        self.assertEqual(sum("Bedrock" in w for w in topo.meta["warnings"]), 1)

    def test_unreadable_policies_fall_back_to_usage_with_one_warning(self):
        self.overrides[("iam", None)] = lambda c: NoDetails(c)
        topo = self.collect_ai(usage=["amazon.nova-e"])
        self.assertEqual(sorted(self.models(topo)), ["amazon.nova-e"])
        self.assertEqual(sum("Bedrock grants" in w for w in topo.meta["warnings"]), 1)

    def test_is_enabled(self):
        self.assertTrue(aws_ai.is_enabled(available()))
        self.assertTrue(aws_ai.is_enabled(available(agreementAvailability=None)))
        for bad in ({"authorizationStatus": "NOT_AUTHORIZED"},
                    {"entitlementAvailability": "NOT_AVAILABLE"},
                    {"regionAvailability": "NOT_AVAILABLE"},
                    {"agreementAvailability": {"status": "NOT_AVAILABLE"}}):
            self.assertFalse(aws_ai.is_enabled(available(**bad)), bad)

    def test_base_model_id(self):
        self.assertEqual([aws_ai.base_model_id(m) for m in (
            "us.anthropic.claude-a", "global.anthropic.claude-a", "anthropic.claude-a",
            "us-gov.anthropic.claude-a", "us")],
            ["anthropic.claude-a"] * 4 + ["us"])

    def test_output_is_deterministic(self):
        self.grant("app", invoke_policy(foundation("amazon.titan-b")))
        first = self.collect_ai(usage=["amazon.nova-e"])
        again = self.collect_ai(usage=["amazon.nova-e"])
        again.meta["collected_at"] = first.meta["collected_at"]
        self.assertEqual(first.dumps(), again.dumps())


def trust(*principals, service=None):
    statements = [{"Effect": "Allow", "Action": "sts:AssumeRole",
                   "Principal": {"AWS": list(principals)} if principals else {}}]
    if service:
        statements.append({"Effect": "Allow", "Action": "sts:AssumeRole",
                           "Principal": {"Service": service}})
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


class NoDetails(Overlay):
    """An IAM client whose account-details call is refused."""

    def get_paginator(self, operation):
        if operation == "get_account_authorization_details":
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, operation)
        return self._client.get_paginator(operation)


class IamTest(AwsTestCase):
    def build(self):
        iam = boto3.client("iam", region_name=REGION)
        iam.create_role(RoleName="deploy",
                        AssumeRolePolicyDocument=trust(f"arn:aws:iam::{OTHER}:root",
                                                       service="ec2.amazonaws.com"),
                        Path="/ci/", Tags=[{"Key": "team", "Value": "platform"}])
        # moto doesn't preload AWS's managed policies; a same-named one stands in.
        arn = iam.create_policy(PolicyName="AdministratorAccess", PolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": "*", "Resource": "*"}]}))["Policy"]["Arn"]
        iam.attach_role_policy(RoleName="deploy", PolicyArn=arn)
        iam.put_role_policy(RoleName="deploy", PolicyName="inline-one",
                            PolicyDocument=json.dumps(
                                {"Version": "2012-10-17", "Statement": [
                                    {"Effect": "Allow", "Action": "s3:ListBucket",
                                     "Resource": "*"}]}))
        iam.create_role(RoleName="open", AssumeRolePolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}]}))
        iam.create_role(RoleName="AWSServiceRoleForThing", Path="/aws-service-role/thing/",
                        AssumeRolePolicyDocument=trust(service="thing.amazonaws.com"))
        iam.create_role(RoleName="AWSReservedSSO_Admin_abc",
                        Path="/aws-reserved/sso.amazonaws.com/",
                        AssumeRolePolicyDocument=trust(service="sso.amazonaws.com"))
        iam.create_user(UserName="alice")
        iam.create_access_key(UserName="alice")
        iam.create_user(UserName="bob")
        return iam

    def collect_iam(self, *extra):
        self.build()
        return self.collect("--region", REGION, "--iam", "--skip-network", *extra)

    def test_custom_roles_and_users(self):
        topo = self.collect_iam()
        account = f"aws:account:{ACCOUNT}"
        roles = sorted(n.label for n in topo.by_kind("iam-role"))
        self.assertEqual(roles, ["deploy", "open"])
        deploy = topo.nodes[f"aws:iam-role:{ACCOUNT}:deploy"]
        box = topo.nodes[f"aws:iam:{ACCOUNT}"]
        self.assertEqual((box.kind, box.label, box.parent), ("iam", "IAM", account))
        self.assertEqual(box.attrs["summary"], "2 roles, 2 users")
        self.assertEqual(deploy.parent, box.id)
        self.assertEqual(topo.nodes[f"aws:iam-user:{ACCOUNT}:alice"].parent, box.id)
        self.assertEqual(deploy.attrs["path"], "/ci/")
        self.assertEqual(deploy.attrs["trusted_accounts"], [OTHER])
        self.assertEqual(deploy.attrs["trusted_services"], ["ec2.amazonaws.com"])
        self.assertEqual(deploy.attrs["attached_policies"], ["AdministratorAccess"])
        self.assertEqual(deploy.attrs["inline_policies"], ["inline-one"])
        self.assertIs(deploy.attrs["admin_access"], True)
        self.assertNotIn("tags", deploy.attrs)   # tags are opt-in
        self.assertIs(topo.nodes[f"aws:iam-role:{ACCOUNT}:open"].attrs["trusts_everyone"], True)

    def test_users_have_mfa_and_key_facts(self):
        topo = self.collect_iam()
        alice = topo.nodes[f"aws:iam-user:{ACCOUNT}:alice"]
        bob = topo.nodes[f"aws:iam-user:{ACCOUNT}:bob"]
        self.assertEqual(alice.attrs["active_access_keys"], 1)
        self.assertEqual(alice.attrs["access_keys"][0]["status"], "Active")
        self.assertIs(alice.attrs["mfa_enabled"], False)
        self.assertEqual(bob.attrs["active_access_keys"], 0)
        self.assertNotIn("AccessKeyId", json.dumps(topo.to_dict()))

    def test_tags_and_aws_owned_roles_on_request(self):
        topo = self.collect_iam("--iam-all", "--include", "tags")
        names = sorted(n.label for n in topo.by_kind("iam-role"))
        self.assertEqual(names, ["AWSReservedSSO_Admin_abc", "AWSServiceRoleForThing", "deploy",
                                 "open"])
        self.assertIs(topo.nodes[f"aws:iam-role:{ACCOUNT}:AWSServiceRoleForThing"
                                 ].attrs["aws_owned"], True)
        self.assertEqual(topo.nodes[f"aws:iam-role:{ACCOUNT}:deploy"].attrs["tags"],
                         {"team": "platform"})

    def test_fallback_when_account_details_are_denied(self):
        self.overrides[("iam", None)] = lambda c: NoDetails(c)
        topo = self.collect_iam()
        self.assertEqual(sorted(n.label for n in topo.by_kind("iam-role")), ["deploy", "open"])
        self.assertEqual(sorted(n.label for n in topo.by_kind("iam-user")), ["alice", "bob"])
        self.assertTrue(any("cannot get IAM account details (AccessDenied)" in w
                            for w in topo.meta["warnings"]))

    def test_denied_user_security_reads_warn_once(self):
        self.overrides[("iam", None)] = lambda c: Overlay(
            c, list_mfa_devices=denied("ListMFADevices"))
        topo = self.collect_iam()
        self.assertEqual(sum("MFA and access keys" in w for w in topo.meta["warnings"]), 1)
        self.assertNotIn("mfa_enabled", topo.nodes[f"aws:iam-user:{ACCOUNT}:alice"].attrs)

    def test_no_network_collection_means_no_regions(self):
        topo = self.collect_iam()
        self.assertEqual(topo.by_kind("region"), [])
        self.assertEqual(topo.by_kind("vpc"), [])
        self.assertEqual(topo.meta["regions"], [])


class TrustTest(unittest.TestCase):
    def test_summary(self):
        doc = json.loads(trust(f"arn:aws:iam::{OTHER}:role/ci/builder", "111122223333", "*",
                               service="lambda.amazonaws.com"))
        summary = aws_iam.trust_summary(doc, ACCOUNT)
        self.assertEqual(summary["trusted_accounts"], [OTHER])
        self.assertEqual(summary["trusted_principals"], [
            {"account": OTHER, "type": "role", "name": "builder"},
            {"account": OTHER, "type": "root"}])
        self.assertIs(summary["trusts_everyone"], True)
        self.assertEqual(summary["trusted_services"], ["lambda.amazonaws.com"])

    def test_own_account_trust(self):
        doc = json.loads(trust(f"arn:aws:iam::{ACCOUNT}:root",
                               f"arn:aws:iam::{ACCOUNT}:role/app-instance"))
        summary = aws_iam.trust_summary(doc, ACCOUNT)
        self.assertIsNone(summary["trusted_accounts"])
        self.assertIs(summary["trusts_own_account"], True)
        self.assertEqual(summary["trusted_principals"],
                         [{"account": ACCOUNT, "type": "role", "name": "app-instance"}])

    def test_same_account_role_links_but_not_account_links(self):
        t = Topology()
        t.add_node(f"aws:account:{ACCOUNT}", "account", account_id=ACCOUNT)
        t.add_node(f"aws:iam-role:{ACCOUNT}:app-instance", "iam-role",
                   parent=f"aws:account:{ACCOUNT}", account_id=ACCOUNT)
        t.add_node(f"aws:iam-role:{ACCOUNT}:bedrock", "iam-role", parent=f"aws:account:{ACCOUNT}",
                   account_id=ACCOUNT, trusted_principals=[
                       {"account": ACCOUNT, "type": "role", "name": "app-instance"},
                       {"account": ACCOUNT, "type": "role", "name": "not-collected"}])
        aws_iam.add_trust_links(t)
        self.assertEqual([(l.source, l.target) for l in t.links.values()],
                         [(f"aws:iam-role:{ACCOUNT}:app-instance", f"aws:iam-role:{ACCOUNT}:bedrock")])

    def test_federated_deny_and_urlencoded_documents(self):
        doc = {"Statement": [
            {"Effect": "Allow", "Principal": {"Federated": "arn:aws:iam::1:oidc-provider/x"}},
            {"Effect": "Deny", "Principal": {"AWS": f"arn:aws:iam::{OTHER}:root"}}]}
        summary = aws_iam.trust_summary(doc, ACCOUNT)
        self.assertEqual(summary["trusted_federated"], ["arn:aws:iam::1:oidc-provider/x"])
        self.assertIsNone(summary["trusted_accounts"])
        from urllib.parse import quote
        again = aws_iam.trust_summary(quote(trust(f"arn:aws:iam::{OTHER}:root")), ACCOUNT)
        self.assertEqual(again["trusted_accounts"], [OTHER])
        self.assertEqual(aws_iam.trust_summary("not json", ACCOUNT)["trusted_accounts"], None)

    def test_links_go_from_the_trusted_principal_to_the_role(self):
        t = Topology()
        for account in (ACCOUNT, OTHER, "999999999999"):
            t.add_node(f"aws:account:{account}", "account", account_id=account)
        t.add_node(f"aws:iam-role:{OTHER}:builder", "iam-role", parent=f"aws:account:{OTHER}")
        t.add_node(f"aws:iam-role:{ACCOUNT}:deploy", "iam-role", parent=f"aws:account:{ACCOUNT}",
                   trusted_principals=[
                       {"account": OTHER, "type": "role", "name": "builder"},
                       {"account": "999999999999", "type": "root"},
                       {"account": "888888888888", "type": "root"}])
        aws_iam.add_trust_links(t)
        links = {(l.source, l.target): l for l in t.links.values()}
        self.assertEqual(set(links), {
            (f"aws:iam-role:{OTHER}:builder", f"aws:iam-role:{ACCOUNT}:deploy"),
            ("aws:account:999999999999", f"aws:iam-role:{ACCOUNT}:deploy")})
        self.assertTrue(all(l.directed and l.kind == "iam-trust" for l in links.values()))
        self.assertEqual(t.validate(), [])


class EksTest(AwsTestCase):
    def build(self, region=REGION):
        ec2 = boto3.client("ec2", region_name=region)
        eks = boto3.client("eks", region_name=region)
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24",
                                     AvailabilityZone=f"{region}{az}")["Subnet"]["SubnetId"]
                   for i, az in ((1, "a"), (2, "b"))]
        eks.create_cluster(name="platform", version="1.30",
                           roleArn=f"arn:aws:iam::{ACCOUNT}:role/eks",
                           resourcesVpcConfig={"subnetIds": subnets,
                                               "endpointPublicAccess": True,
                                               "endpointPrivateAccess": False},
                           tags={"team": "platform"})
        eks.create_nodegroup(clusterName="platform", nodegroupName="workers",
                             scalingConfig={"minSize": 1, "maxSize": 3, "desiredSize": 2},
                             subnets=subnets, nodeRole=f"arn:aws:iam::{ACCOUNT}:role/node",
                             instanceTypes=["t3.large"])
        return vpc, subnets

    def with_vpc_id(self, vpc):
        """moto leaves vpcId out of the cluster's VPC config; AWS includes it."""
        def describe(c):
            def answer(name):
                cluster = c.describe_cluster(name=name)
                cluster["cluster"]["resourcesVpcConfig"]["vpcId"] = vpc
                return cluster
            return Overlay(c, describe_cluster=answer)
        self.overrides[("eks", None)] = describe

    def test_cluster_sits_in_its_vpc_with_its_facts(self):
        vpc, subnets = self.build()
        self.with_vpc_id(vpc)
        topo = self.collect("--region", REGION, "--eks")
        cluster = topo.nodes[f"aws:eks-cluster:{ACCOUNT}:{REGION}:platform"]
        self.assertEqual(cluster.parent, f"aws:vpc:{vpc}")
        self.assertEqual(cluster.label, "platform")
        self.assertEqual((cluster.attrs["version"], cluster.attrs["status"]), ("1.30", "ACTIVE"))
        self.assertIs(cluster.attrs["endpoint_public_access"], True)
        self.assertIs(cluster.attrs["endpoint_private_access"], False)
        self.assertEqual(cluster.attrs["subnet_ids"], sorted(subnets))
        self.assertEqual(cluster.attrs["vpc_id"], vpc)
        group = cluster.attrs["nodegroups"][0]
        self.assertEqual((group["name"], group["instance_types"], group["desired"]),
                         ("workers", ["t3.large"], 2))
        self.assertNotIn("tags", cluster.attrs)
        self.assertNotIn("endpoint", cluster.attrs)   # the API hostname isn't kept

    def test_tags_on_request_and_off_by_default(self):
        self.build()
        topo = self.collect("--region", REGION, "--eks", "--include", "tags")
        cluster = topo.nodes[f"aws:eks-cluster:{ACCOUNT}:{REGION}:platform"]
        self.assertEqual(cluster.attrs["tags"], {"team": "platform"})

    def test_no_eks_flag_no_clusters(self):
        self.build()
        self.assertEqual(self.collect("--region", REGION).by_kind("eks-cluster"), [])

    def test_denied_listing_is_one_warning(self):
        self.build()
        self.overrides[("eks", None)] = lambda c: Overlay(
            c, list_clusters=denied("ListClusters"))
        topo = self.collect("--region", REGION, "--eks")
        self.assertEqual(topo.by_kind("eks-cluster"), [])
        self.assertEqual(sum("EKS" in w for w in topo.meta["warnings"]), 1)

    def test_a_cluster_that_cannot_be_described_is_left_out_with_a_warning(self):
        self.build()
        self.overrides[("eks", None)] = lambda c: Overlay(
            c, describe_cluster=denied("DescribeCluster"))
        topo = self.collect("--region", REGION, "--eks")
        self.assertEqual(topo.by_kind("eks-cluster"), [])
        self.assertTrue(any("cannot describe EKS cluster platform" in w
                            for w in topo.meta["warnings"]))


class FlagsTest(AwsTestCase):
    def test_help_lists_the_flags(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            parse("--help")
        for flag in ("--ai", "--iam", "--iam-all", "--eks", "--skip-network"):
            self.assertIn(flag, out.getvalue())

    def test_wanted_flags(self):
        self.assertEqual(aws._wanted(parse()), frozenset())
        self.assertEqual(aws._wanted(parse("--ai", "--iam", "--iam-all", "--eks",
                                           "--skip-network")),
                         frozenset({"ai", "iam", "iam-all", "eks", "skip-network"}))

    def test_skip_network_leaves_out_vpcs(self):
        self.build_vpc()
        topo = self.collect("--region", REGION, "--skip-network", "--iam")
        self.assertEqual(topo.by_kind("vpc"), [])
        topo = self.collect("--region", REGION)
        self.assertEqual(len(topo.by_kind("vpc")), 2)   # the built one and the default


class PartitionTest(AwsTestCase):
    def test_helpers(self):
        self.assertEqual([aws.partition_of_region(r) for r in
                          ("us-east-1", "us-gov-west-1", "us-gov-east-1", "cn-north-1")],
                         ["aws", "aws-us-gov", "aws-us-gov", "aws-cn"])
        self.assertEqual(aws.fallback_region("us-gov-east-1"), "us-gov-west-1")
        self.assertEqual(aws.fallback_region("cn-northwest-1"), "cn-north-1")
        self.assertEqual(aws.fallback_region("eu-west-1"), "us-east-1")
        self.assertEqual(aws.fallback_region(), "us-east-1")

    def test_service_lookup_uses_the_regions_own_partition(self):
        seen = []

        class Session:
            def get_available_regions(self, service, partition_name="aws"):
                seen.append(partition_name)
                return {"aws": ["us-east-1"], "aws-us-gov": ["us-gov-west-1"]}.get(partition_name, [])

        aws_ai.service_regions.cache_clear()
        try:
            with mock.patch("boto3.session.Session", Session):
                self.assertTrue(aws_ai.in_region("bedrock", "us-gov-west-1"))
                self.assertFalse(aws_ai.in_region("bedrock", "us-gov-east-1"))
                self.assertTrue(aws_ai.in_region("bedrock", "us-east-1"))
                self.assertFalse(aws_ai.in_region("bedrock", "us-west-2"))
        finally:
            aws_ai.service_regions.cache_clear()
        self.assertEqual(set(seen), {"aws", "aws-us-gov"})

    def test_collects_in_a_govcloud_region(self):
        ec2 = boto3.client("ec2", region_name="us-gov-west-1")
        vpc = ec2.create_vpc(CidrBlock="10.7.0.0/16")["Vpc"]["VpcId"]
        topo = self.collect("--region", "us-gov-west-1", "--iam", "--eks")
        self.assertEqual(topo.meta["regions"], ["us-gov-west-1"])
        self.assertEqual(topo.nodes[f"aws:vpc:{vpc}"].parent, f"aws:region:{ACCOUNT}:us-gov-west-1")
        self.assertNotIn("warnings", topo.meta)

    def test_home_region_is_probed_per_partition_when_none_is_given(self):
        tried = []

        class Session:
            def client(self, service, region_name=None, config=None):
                tried.append(region_name)
                if not region_name.startswith("us-gov-"):
                    raise ClientError({"Error": {"Code": "InvalidClientTokenId", "Message": "x"}},
                                      "GetCallerIdentity")
                return Overlay(None, get_caller_identity={"Account": ACCOUNT,
                                                          "Arn": "arn:aws-us-gov:iam::1:user/u"})

        with mock.patch.object(aws, "_new_client", lambda s, svc, r: s.client(svc, r)):
            home, identity = aws.probe_home(Session())
            self.assertEqual((home, tried), ("us-gov-west-1", ["us-east-1", "us-gov-west-1"]))
            self.assertEqual(identity["Arn"].split(":")[1], "aws-us-gov")
            self.assertEqual(aws.probe_home(Session(), "us-gov-east-1")[0], "us-gov-east-1")
            with self.assertRaises(ClientError):
                aws.probe_home(Session(), "us-east-1")


def scp_denied(operation):
    def raise_error(**_):
        raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": (
            "You are not authorized to perform this operation. User: arn:aws:sts::1:assumed-role/r/s "
            f"is not authorized to perform: ec2:{operation} with an explicit deny in a service "
            "control policy")}}, operation)
    return raise_error


class ScpTest(AwsTestCase):
    def test_a_region_an_scp_blocks_is_marked_not_just_warned_about(self):
        ids = self.build_vpc()
        self.overrides[("ec2", "us-west-2")] = lambda c: Overlay(
            c, describe_vpcs=scp_denied("DescribeVpcs"))
        topo = self.collect("--region", REGION, "--region", "us-west-2")
        blocked = topo.nodes[f"aws:region:{ACCOUNT}:us-west-2"]
        self.assertEqual(blocked.attrs["scp_denied"], ["EC2"])
        self.assertIs(blocked.attrs["empty"], True)
        self.assertNotIn("scp_denied", topo.nodes[f"aws:region:{ACCOUNT}:{REGION}"].attrs)
        self.assertIn(f"aws:vpc:{ids['vpc']}", topo.nodes)
        self.assertEqual([w for w in topo.meta["warnings"] if "us-west-2" in w],
                         [f"aws {ACCOUNT} us-west-2: EC2 is blocked here by a service control "
                          "policy"])

    def test_an_ordinary_denial_is_not_called_an_scp(self):
        self.overrides[("ec2", REGION)] = lambda c: Overlay(
            c, describe_vpcs=denied("DescribeVpcs", "UnauthorizedOperation"))
        topo = self.collect("--region", REGION)
        self.assertNotIn("scp_denied", topo.nodes[f"aws:region:{ACCOUNT}:{REGION}"].attrs)
        self.assertTrue(any("cannot describe VPCs" in w for w in topo.meta["warnings"]))

    def test_label_names_what_is_blocked(self):
        from topomapper.model import Node
        from topomapper.render.labels import node_lines
        node = Node("aws:region:1:eu-west-1", "region", "eu-west-1",
                    attrs={"region": "eu-west-1", "scp_denied": ["EC2", "EKS"], "empty": True})
        self.assertEqual(node_lines(node)[1:], ["SCP blocks EC2, EKS"])


if __name__ == "__main__":
    unittest.main()
