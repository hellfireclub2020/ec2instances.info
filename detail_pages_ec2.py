import mako.template
import mako.lookup
import mako.exceptions
import io
import json
import datetime
import os
import csv
import bisect
import yaml
import re


def storage(sattrs, imap):
    if not sattrs:
        return []
    storage_details = []
    for s, v in sattrs.items():
        try:
            display = imap[s]
            display["value"] = v
            storage_details.append(format_attribute(display))
        except KeyError:
            # We chose not to represent this storage attribute
            continue
    return storage_details


def initial_prices(i):
    try:
        od = i["Pricing"]["us-east-1"]["linux"]["ondemand"]
    except:
        # If prices are not available for us-east-1 it means this is a custom instance of some kind
        return ["'N/A'", "'N/A'", "'N/A'", "'N/A'"]

    od = i["Pricing"]["us-east-1"]["linux"]["ondemand"]
    spot = i["Pricing"]["us-east-1"]["linux"]["spot"]
    try:
        _1yr = i["Pricing"]["us-east-1"]["linux"]["_1yr"]["Standard.noUpfront"]
        _3yr = i["Pricing"]["us-east-1"]["linux"]["_3yr"]["Standard.noUpfront"]
    except:
        # If we can't get a reservation, likely a previous generation
        _1yr = "'N/A'"
        _3yr = "'N/A'"

    return [od, spot, _1yr, _3yr]


def description(id):
    name = id["Amazon"][1]["value"]
    family_category = id["Amazon"][2]["value"].lower()
    cpus = id["Compute"][0]["value"]
    memory = id["Compute"][1]["value"]
    bandwidth = id["Networking"][0]["value"]

    # Some instances say "Low to moderate" for bandwidth, ignore them
    try:
        bandwidth = f' and {int(id["Networking"][0]["value"])} Gibps of bandwidth.'
    except:
        bandwidth = "."

    return f"The {name} instance is a {family_category} instance with {cpus} vCPUs, {memory} GiB of memory{bandwidth}"


def community(instance, links):
    # TODO: not the most efficient with many links
    for l in links:
        k, linklist = next(iter(l.items()))
        if k == instance:
            return linklist["links"]
    return []


def unavailable_instances(itype, instance_details):
    data_file = "meta/regions_aws.yaml"
    ec2_os = {
        "linux": "Linux",
        "mswin": "Windows",
        "rhel": "Red Hat",
        "sles": "SUSE",
        "linuxSQL": "Linux SQL Server",
        "linuxSQLWeb": "Linux SQL Server for Web",
        "linuxSQLEnterprise": "Linux SQL Enterprise",
        "mswinSQL": "Windows SQL Server",
        "mswinSQLWeb": "Windows SQL Web",
        "mswinSQLEnterprise": "Windows SQL Enterprise",
        "rhelSQL": "Red Hat SQL Server",
        "rhelSQLWeb": "Red Hat SQL Web",
        "rhelSQLEnterprise": "Red Hat SQL Enterprise",
    }

    denylist = []
    with open(data_file, "r") as f:
        aws_regions = yaml.safe_load(f)
        instance_regions = instance_details["Pricing"].keys()

        # If there is no price for a region and os, then it is unavailable
        for r in aws_regions:
            if r not in instance_regions:
                # print("Found that {} is not available in {}".format(itype, r))
                denylist.append([aws_regions[r], r, "All", "*"])
            else:
                instance_regions_oss = instance_details["Pricing"][r].keys()
                denylist.extend(
                    [aws_regions[r], r, value, os]
                    for os, value in ec2_os.items()
                    if os not in instance_regions_oss
                )

    return denylist


def assemble_the_families(instances):
    # Build 2 lists - one where we can lookup what family an instance belongs to
    # and another where we can get the family and see what the members are
    instance_fam_map = {}
    families = {}
    variant_families = {}

    for i in instances:
        name = i["instance_type"]
        itype, suffix = name.split(".")
        variant = itype[:2]

        if variant not in variant_families:
            variant_families[variant] = [[itype, name]]
        else:
            dupe = 0
            for v, _ in variant_families[variant]:
                if v == itype:
                    dupe = 1
            if not dupe:
                variant_families[variant].append([itype, name])

        member = {"name": name, "cpus": int(i["vCPU"]), "memory": int(i["memory"])}
        if itype not in instance_fam_map:
            instance_fam_map[itype] = [member]
        else:
            instance_fam_map[itype].append(member)

        # The second list, where we will get the family from knowing the instance
        families[name] = itype

    # Order the families by number of cpus so they display this way on the webpage
    for f, ilist in instance_fam_map.items():
        ilist.sort(key=lambda x: x["cpus"])
        # Move the metal instances to the end of the list
        for j in ilist:
            if j["name"].endswith("metal"):
                ilist.remove(j)
                ilist.append(j)
        instance_fam_map[f] = ilist

    # for debugging: print(json.dumps(instance_fam_map, indent=4))
    return instance_fam_map, families, variant_families


def prices(pricing):
    display_prices = {}
    for region, p in pricing.items():
        display_prices[region] = {}

        for os, _p in p.items():
            display_prices[region][os] = {}

            if os in ["ebs", "emr"]:
                continue

            # Doing a lot of work to deal with prices having up to 6 places
            # after the decimal, as well as prices not existing for all regions
            # and operating systems.
            try:
                display_prices[region][os]["ondemand"] = _p["ondemand"]
            except KeyError:
                display_prices[region][os]["ondemand"] = "N/A"

            try:
                display_prices[region][os]["spot"] = _p["spot_max"]
            except KeyError:
                display_prices[region][os]["spot"] = "N/A"

            try:
                reserved = {k[7:]: v for k, v in _p["reserved"].items() if "Term1" in k}
                display_prices[region][os]["_1yr"] = reserved
            except KeyError:
                display_prices[region][os]["_1yr"] = "N/A"

            try:
                reserved = {k[7:]: v for k, v in _p["reserved"].items() if "Term3" in k}
                display_prices[region][os]["_3yr"] = reserved
            except KeyError:
                display_prices[region][os]["_3yr"] = "N/A"

    return display_prices


def load_service_attributes():
    # This CSV file contains nicely formatted names, styling hints,
    # and order of display for instance attributes
    data_file = "meta/service_attributes_ec2.csv"

    display_map = {}
    with open(data_file, "r") as f:
        reader = csv.reader(f)

        for i, row in enumerate(reader):
            cloud_key = row[0]
            if i == 0:
                # Skip the header
                continue
            else:
                category = row[2]

            display_map[cloud_key] = {
                "cloud_key": cloud_key,
                "display_name": row[1],
                "category": category,
                "order": row[3],
                "style": row[4],
                "regex": row[5],
                "value": None,
                "variant_family": row[1][:2],
            }


    return display_map


def format_attribute(display):

    if display["regex"]:
        toparse = str(display["value"])
        regex = str(display["regex"])
        if match := re.search(regex, toparse):
            display["value"] = match.group()
            # else:
            #     print("No match found for {} with regex {}".format(toparse, regex))

    if display["style"]:
        v = str(display["value"]).lower()
        if v in {"false", "0", "none"}:
            display["style"] = "value value-false"
        elif v == "current":
            display["style"] = "value value-current"
        elif v == "previous":
            display["style"] = "value value-previous"
        else:
            display["style"] = "value value-true"

    return display


def map_ec2_attributes(i, imap):
    # For now, manually transform the instance data we receive from AWS
    # into the format we want to render. Later we can create this in YAML
    # and use a standard function that maps names
    categories = [
        "Compute",
        "Networking",
        "Storage",
        "Amazon",
        "Not Shown",
    ]
    special_attributes = [
        "pricing",
        "storage",
        "vpc",
    ]

    # Group attributes into categories which are then displayed in sections on the page
    instance_details = {c: [] for c in categories}
    for j, k in i.items():
        # Some attributes like storage have nested values that we handle differently
        if j not in special_attributes:
            display = imap[j]
            display["value"] = k
            instance_details[display["category"]].append(format_attribute(display))

    # Special cases
    instance_details["Storage"].extend(storage(i["storage"], imap))

    for c in categories:
        instance_details[c].sort(key=lambda x: int(x["order"]))

    # Pricing widget
    instance_details["Pricing"] = prices(i["pricing"])

    # for debugging: print(json.dumps(instance_details, indent=4))
    return instance_details


def build_detail_pages_ec2(instances, destination_file):
    # Extract which service these instances belong to, for example EC2 is loaded at /
    service_path = destination_file.split("/")[1]
    data_file = "community_contributions.yaml"
    stream = open(data_file, "r")
    community_data = list(yaml.load_all(stream, Loader=yaml.SafeLoader))

    # Find the right path to write these files to. There is a .gitignore file
    # in each directory so that these generated files are not committed
    subdir = os.path.join("www", "aws", "ec2")
    if service_path != "index.html":
        subdir = os.path.join("www", "aws", service_path)

    ifam, fam_lookup, variants = assemble_the_families(instances)
    imap = load_service_attributes()

    lookup = mako.lookup.TemplateLookup(directories=["."])
    template = mako.template.Template(
        filename="in/instance-type.html.mako", lookup=lookup
    )

    # To add more data to a single instance page, do so inside this loop
    could_not_render = []
    sitemap = []
    for i in instances:
        instance_type = i["instance_type"]

        instance_page = os.path.join(subdir, f"{instance_type}.html")
        instance_details = map_ec2_attributes(i, imap)
        fam = fam_lookup[instance_type]
        fam_members = ifam[fam]
        idescription = description(instance_details)
        links = community(instance_type, community_data)
        denylist = unavailable_instances(instance_type, instance_details)
        defaults = initial_prices(instance_details)

        print(f"Rendering {instance_type} to detail page {instance_page}...")
        with io.open(instance_page, "w+", encoding="utf-8") as fh:
            try:
                fh.write(
                    template.render(
                        i=instance_details,
                        family=fam_members,
                        description=idescription,
                        links=links,
                        unavailable=denylist,
                        defaults=defaults,
                        variants=variants[instance_type[:2]],
                    )
                )

                sitemap.append(instance_page)
            except:
                render_err = mako.exceptions.text_error_template().render()
                err = {"e": f"ERROR for {instance_type}", "t": render_err}

                could_not_render.append(err)

    [print(err["e"], f'{err["t"]}') for err in could_not_render]
    [print(page["e"]) for page in could_not_render]

    return sitemap
