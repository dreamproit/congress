# Downloads documents from GPO's GovInfo.gov site, using sitemaps
# to efficiently determine what needs to be updated. See
# https://www.govinfo.gov/sitemaps for a list of collections.
# This service was formerly called "Fdsys."
#
# usc-run govinfo --collections=BILLS,STATUTE,...
# Download bill text (from the BILLS collection; there's also a bulk
# data BILLS collection but it has less in it), the Statues at Large,
# and other documents from GovInfo.gov's non-bulk-data collections.
#
# usc-run govinfo --bulkdata=BILLSTATUS,FR,...
# Download bill status, the Federal Register, and other documents
# from GovInfo.gov's bulk data collections. (The BILLS collection occurs
# both as a regular collection (bill text in multiple formats) and as
# a bulk data collection (just XML starting recently). Use --bulkdata=BILLS
# to get the bulk data collection.)
#
#   Options:
#
#   --years=2001[,2002,2004]
#   Comma-separated list of years to download from. Applies to collections
#   that are divided by year.
#
#   --congress=113[,114]
#   Comma-separated list of congresses to download from. Applies to bulk
#   data collections like BILLSTATUS that are grouped by Congress + Bill Type.
#
#   --extract=mods,pdf,text,xml,premis
#   Extract the MODS, PDF, text, XML, or PREMIS file associated
#   with each package from the downloaded package ZIP file.
#
#   --filter="regex"
#   Only stores files that match the regex. Regular collections
#   are matched against the package name (i.e. BILLS-113hconres66ih)
#   while bulk data items are matched against the their file path
#   (i.e. 113/1/hconres/BILLS-113hconres66ih.xml).
#
#   --cached|--force
#   Always/never use the cache.

import json
import logging
import multiprocessing
import os
import os.path
import pathlib
import re
import zipfile

from lxml import etree, html
import rtyaml

from congress.common.constants.congress import CongressConstants
from core.tasks import utils

logger = logging.getLogger(CongressConstants.CONGRESS_DEFAULT_LOGGER_NAME.value)


# globals
GOVINFO_BASE_URL = "https://www.govinfo.gov/"
COLLECTION_BASE_URL = GOVINFO_BASE_URL + "app/details/"
BULKDATA_BASE_URL = GOVINFO_BASE_URL + "bulkdata/"
COLLECTION_SITEMAPINDEX_PATTERN = GOVINFO_BASE_URL + "sitemap/{collection}_sitemap_index.xml"
BULKDATA_SITEMAPINDEX_PATTERN = GOVINFO_BASE_URL + "sitemap/bulkdata/{collection}/sitemapindex.xml"
FDSYS_BILLSTATUS_FILENAME = "fdsys_billstatus.xml"

# for xpath
ns = {"x": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# Main entry point


def run(options):
    # Process sitemaps.
    logger.info(f'Running govinfo with options: "{options}".')
    for collection in sorted(options.get("collections", "").split(",")):
        if collection != "":
            update_sitemap(
                COLLECTION_SITEMAPINDEX_PATTERN.format(collection=collection),
                None,
                [],
                options,
            )
    for collection in sorted(options.get("bulkdata", "").split(",")):
        if collection != "":
            update_sitemap(
                BULKDATA_SITEMAPINDEX_PATTERN.format(collection=collection),
                None,
                [],
                options,
            )


def update_sitemap(url, current_lastmod, how_we_got_here, options):
    """Updates the local cache of a sitemap file."""

    # Skip if the year or congress flags are set and this sitemap is
    # not for that year or Congress.
    if should_skip_sitemap(url, options):
        return []

    # For debugging, remember what URLs we are stepping through.
    how_we_got_here = how_we_got_here + [url]

    # Get the file paths to cache:
    # * the sitemap XML for future runs
    # * its <lastmod> date (which comes from the parent sitemap) so we know if we need to re-download it now
    # * the <lastmod> dates of the packages listed in this sitemap so we know if we need to re-download any package files # noqa
    cache_file = get_sitemap_cache_file(url)
    cache_file = os.path.join("govinfo/sitemap", cache_file, "sitemap.xml")
    lastmod_cache_file = cache_file.replace(".xml", "-lastmod.yaml")
    lastmod_cache_file = os.path.join(utils.cache_dir(), lastmod_cache_file)
    if not os.path.exists(lastmod_cache_file):
        lastmod_cache = {}
    else:
        with open(lastmod_cache_file) as f:
            lastmod_cache = rtyaml.load(f)

    try:
        return update_sitemap2(url, current_lastmod, how_we_got_here, options, lastmod_cache, cache_file)
    finally:
        # Write the updated last modified dates to disk so we know the next time whether
        # we need to fetch the files. If we didn't download anything, no need to write an
        # empty file.
        with utils.NoInterrupt():
            with open(lastmod_cache_file, "w") as f:
                rtyaml.dump(lastmod_cache, f)


def update_sitemap2(url, current_lastmod, how_we_got_here, options, lastmod_cache, cache_file):
    # Return a list of files we downloaded.
    results = []

    # Download anew if the current_lastmod doesn't match the stored lastmod
    # in our cache, and if --cache is not specified. Or if --force is given.
    # If we're not downloading it, load it from disk because we still have
    # to process each sitemap to ensure we've downloaded all of the package
    # files the user wants.
    download = should_download_sitemap(lastmod_cache.get("lastmod"), current_lastmod, options)

    # Download, or just retreive from cache.
    if download:
        logger.info("Downloading: %s" % url)
    body = utils.download(url, cache_file, utils.merge(options, {'force': download, 'binary': True}))
    if not body:
        logger.error("Failed to download %s. Skipping." % url)
        return results

    # If we downloaded a new file, update the lastmod for our cache.
    if download and current_lastmod:
        lastmod_cache["lastmod"] = current_lastmod

    # Load the XML.
    try:
        sitemap = etree.fromstring(body)
    except etree.XMLSyntaxError as e:
        raise Exception("XML syntax error in %s: %s" % (url, str(e)))

    # Process the entries.
    if sitemap.tag == "{http://www.sitemaps.org/schemas/sitemap/0.9}sitemapindex":

        # This is a sitemap index. Process the sitemaps listed in this
        # sitemapindex recursively.
        for node in sitemap.xpath("x:sitemap", namespaces=ns):
            # Get URL and lastmod date of the sitemap.
            url = str(node.xpath("string(x:loc)", namespaces=ns))
            lastmod = str(node.xpath("string(x:lastmod)", namespaces=ns))
            sitemap_results = update_sitemap(url, lastmod, how_we_got_here, options)
            if sitemap_results is not None:
                results = results + sitemap_results

    elif sitemap.tag == "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset":

        # This is a regular sitemap with content items listed.

        # Process the items.
        mirror_package_tasks = []
        mirror_bulkdata_file_tasks = []
        nodes = sitemap.xpath("x:url", namespaces=ns)
        for node in nodes:
            url = str(node.xpath("string(x:loc)", namespaces=ns))
            lastmod = str(node.xpath("string(x:lastmod)", namespaces=ns))

            m = re.match(COLLECTION_BASE_URL + r"([^-]+)-(.*)", url)
            if m:
                collection = m.group(1)
                package_name = m.group(2)
                if options.get("filter") and not re.search(options["filter"], package_name):
                    continue
                mirror_package_tasks.append(
                    [
                        collection,
                        package_name,
                        lastmod,
                        lastmod_cache.setdefault("packages", {}),
                        options,
                    ]
                )
            else:
                # This is a bulk data item. Extract components of the URL.
                m = re.match(BULKDATA_BASE_URL + r"([^/]+)/(.*)", url)
                if not m:
                    raise Exception("Unmatched bulk data file URL (%s) at %s." % (url, "->".join(how_we_got_here)))
                collection = m.group(1)
                item_path = m.group(2)
                if options.get("filter") and not re.search(options["filter"], item_path):
                    continue
                mirror_bulkdata_file_tasks.append([collection, url, item_path, lastmod, options])

        PROCESSES_TO_CREATE = CongressConstants.CONRESS_PROCESSES_TO_CREATE.value
        logger.info(
            f'Processing mirror_package_tasks: "{len(mirror_package_tasks)}" in "{PROCESSES_TO_CREATE}" subprocesses.'
        )
        logger.info(
            f'Processing mirror_bulkdata_file_tasks: "{len(mirror_bulkdata_file_tasks)}" in '
            f'"{PROCESSES_TO_CREATE}" subprocesses.'
        )
        with multiprocessing.Pool(processes=PROCESSES_TO_CREATE) as pool:
            if mirror_bulkdata_file_tasks:
                result = pool.starmap(mirror_bulkdata_file, mirror_bulkdata_file_tasks)
                if isinstance(result, list):
                    results.extend(result)
            if mirror_package_tasks:
                result = pool.starmap(mirror_package, mirror_package_tasks)
                if isinstance(result, list):
                    results.extend(result)
    return results


def should_skip_sitemap(url, options):
    # Don't skip sitemap indexes.
    m = re.match(re.escape(GOVINFO_BASE_URL) + r"sitemap/(\w+)_sitemap_index.xml", url)
    if m:
        return False
    m = re.match(re.escape(GOVINFO_BASE_URL) + r"sitemap/bulkdata/(\w+)/sitemapindex.xml", url)
    if m:
        return False

    year_filter = options.get("years", "").strip()
    congress_filter = options.get("congress", "").strip()

    # Regular collections are grouped by publication year.
    # Which years should we download? All if none is specified.
    m = re.match(re.escape(GOVINFO_BASE_URL) + r"sitemap/(\w+)_(\d+)_sitemap.xml", url)
    if m:
        year = m.group(2)
        if year_filter != "" and year not in year_filter.split(","):
            return True

    # Bulk data collections are grouped into subdirectories that can
    # represent years (as in the FR collection) or other types of groupings
    # like Congress + Bill Type for the BILLSTATUS collection.
    m = re.match(
        re.escape(GOVINFO_BASE_URL) + r"sitemap/bulkdata/(\w+)/(\d+)(.*)/sitemap.xml",
        url,
    )
    if m:
        numeric_grouping = m.group(2)
        if year_filter != "" and numeric_grouping not in year_filter.split(","):
            return True
        if congress_filter != "" and numeric_grouping not in congress_filter.split(","):
            return True

    return False


def get_sitemap_cache_file(url):
    # Where should we store the local cache of the sitemap XML and a file
    # that stores its <lastmod> date for when we last downloaded it? Returns
    # a path relative to the cache root.

    m = re.match(re.escape(GOVINFO_BASE_URL) + r"sitemap/(\w+)_sitemap_index.xml", url)
    if m:
        return m.group(1)

    m = re.match(re.escape(GOVINFO_BASE_URL) + r"sitemap/(\w+)_(\d+)_sitemap.xml", url)
    if m:
        return m.group(1) + "/" + m.group(2)

    m = re.match(re.escape(GOVINFO_BASE_URL) + r"sitemap/bulkdata/(\w+)/sitemapindex.xml", url)
    if m:
        return m.group(1) + "-bulkdata"

    m = re.match(re.escape(GOVINFO_BASE_URL) + r"sitemap/bulkdata/(\w+)/(.+)/sitemap.xml", url)
    if m:
        return m.group(1) + "-bulkdata/" + m.group(2)

    raise ValueError(url)


def should_download_sitemap(lastmod_cache, current_lastmod, options):
    # Download a sitemap or just read from our cache?

    if not current_lastmod:
        # No lastmod is known for this file (it's the root of a sitemap
        # tree - this is the first web request).
        return True

    elif options.get("force", False):
        # User requests downloading everything.
        return True

    elif options.get("cached", False):
        # User requests downloading nothing.
        return False

    else:
        # Download if the lastmod from the parent sitemap doesn't agree with
        # the lastmod stored on disk.
        return current_lastmod != lastmod_cache


# Downloading Packages


def mirror_package(collection, package_name, lastmod, lastmod_cache, options):
    """Create a local mirror of a GovInfo.gov package."""

    # Where should we store the file? Each collection has a different
    # file system layout (for BILLS, we put bill text along where the
    # bills scraper puts bills).
    path = get_output_path(collection, package_name, options)
    if not path:  # should skip
        return []

    # Go to the part of the lastmod_cache for this package.
    lastmod_cache = lastmod_cache.setdefault(package_name, {})
    lastmod_cache = lastmod_cache.setdefault("files", {})

    # Download the package ZIP file. We don't know what formats are available
    # until we download this file, and when we hit particular package files
    # we get 302 HTTP responses, which is uninformative about whether the
    # file is supposed to exist or not. But we can reliably download the ZIP
    # package.
    file_path = os.path.join(path, "package.zip")

    lastmod_cache_file = os.path.splitext(file_path)[0] + "-lastmod.txt"

    # If the file was supposedly downloaded before (i.e. lastmod_cache is
    # not empty) but it is missing, force a re-download by clearing the lastmod cache.
    if lastmod_cache and not os.path.exists(file_path):
        # logger.error("Missing: " + file_path + " (previously: " + repr(lastmod_cache) + ")")
        lastmod_cache.clear()

    if os.path.exists(lastmod_cache_file) and not options.get("force", False):
        lastmod_cache_file_value = utils.read(lastmod_cache_file)
        if lastmod == lastmod_cache_file_value:
            logger.info(
                f'Skipping filepath: "{file_path}" its lastmod: "{lastmod}" is the same as lastmod_cache_file_value: '
                f'"{lastmod_cache_file_value}".'
            )
            return True

    # Download the package ZIP file if it's updated.
    downloaded_files = []
    if mirror_package_zipfile(collection, package_name, file_path, lastmod, lastmod_cache, options):
        downloaded_files.append(file_path)

    # Extract files from the package ZIP file depending on the --extract
    # command-line arguments. We do this even if the package ZIP file has
    # not changed because the --extract arguments might have changed and
    # the caller may want to extract files after having already gotten the
    # package ZIP file.
    try:
        extracted_files = extract_package_files(collection, package_name, file_path, lastmod_cache, options)
        downloaded_files.extend(extracted_files)
    except zipfile.BadZipfile as e:
        # Sometimes files don't download properly. If the ZIP file is
        # corrupt, log the error and delete the file.
        logger.error(str(e) + ". Deleting: " + file_path, exc_info=True)
        os.unlink(file_path)
        lastmod = None
        lastmod_cache_file = None
        return []

    if lastmod and lastmod_cache_file:
        utils.write(lastmod, lastmod_cache_file)

    return downloaded_files


def mirror_package_zipfile(collection, package_name, file_path, lastmod, lastmod_cache, options):
    # Do we already have this file updated?
    if lastmod_cache.get("package") == lastmod:
        if not options.get("force", False):
            return

    # With --cached, skip if the file is already downloaded.
    if os.path.exists(file_path) and options.get("cached", False):
        return

    # Download.
    file_url = GOVINFO_BASE_URL + "content/pkg/{}-{}.zip".format(collection, package_name)
    logger.info("Downloading: " + file_url)
    utils.download(
        file_url,
        file_path,
        utils.merge(
            options,
            {
                'binary': True,
                'force': True,  # decision to cache was made above
                'to_cache': False,
                'needs_content': False,
            },
        ),
    )

    # Update the lastmod of the downloaded file.
    lastmod_cache['package'] = lastmod
    return True


def extract_package_files(collection, package_name, package_file, lastmod_cache, options):
    # Extract files from the package ZIP file depending on the --extract
    # command-line argument. When extracting a file, mark the extracted
    # file's lastmod as the same as the package's lastmod.
    if not pathlib.Path(package_file).exists():
        logger.error(f'Package file: "{package_file}" does not exist.')
        return []

    # Get the formats that the user wants to extract.
    extract_formats = set(format for format in options.get("extract", "").split(",") if format.strip())

    # Make a mapping from file formats to a tuple of the filename found in the package ZIP
    # file and the filename that we will use to store the extracted format locally.
    format_paths = {
        'pdf': (
            "{collection}-{package_name}/pdf/{collection}-{package_name}.pdf",
            "document.pdf",
        ),
        'text': (
            "{collection}-{package_name}/html/{collection}-{package_name}.htm",
            "document.html",
        ),  # text wrapped in HTML!
        'xml': (
            "{collection}-{package_name}/xml/{collection}-{package_name}.xml",
            "document.xml",
        ),
        'mods': ("{collection}-{package_name}/mods.xml", "mods.xml"),
        'premis': ("{collection}-{package_name}/premis.xml", "premis.xml"),
    }

    # Extract only files if the package lastmod is newer than the file's lastmod.
    extract_formats = {
        format
        for format in extract_formats
        if lastmod_cache.get(format) is None or lastmod_cache[format] < lastmod_cache['package']
    }

    # Don't even bother opening the ZIP file if there are no new files to extract.
    if not extract_formats:
        return []

    # Open the package ZIP file and try to extract files with names
    # we recognize.
    extracted_files = []
    with zipfile.ZipFile(package_file) as package:
        for format in extract_formats:
            if format not in format_paths:
                raise ValueError("invalid format: " + format)

            # Construct the expected path in the package ZIP file and the desired local filename.
            package_path, local_path = format_paths[format]
            package_path = package_path.format(collection=collection, package_name=package_name)
            local_path = os.path.join(os.path.dirname(package_file), local_path)

            # Extract it.
            try:
                with package.open(package_path) as f1:
                    with open(local_path, 'wb') as f2:
                        f2.write(f1.read())
            except KeyError:
                # No file of this format is present in this package.
                continue
            finally:
                # Even if the file didn't exist, which is NOT an error condition
                # because not all packages have documents of all formats, update
                # the format's file's lastmod in our cache so that we don't try
                # to extract it again later, unless the package is updated.
                lastmod_cache[format] = lastmod_cache['package']

            logger.info("Extracted: " + local_path)
            extracted_files.append(local_path)

            # The "text" format files are put in an HTML container. Unwrap it into a .txt file.
            if format == "text":
                file_path_text = local_path.replace(".html", ".txt")
                logger.info("Unwrapping HTML to: " + file_path_text)
                with open(local_path) as f1:
                    with open(file_path_text, "wb") as f2:
                        f2.write(unwrap_text_in_html(f1.read()))
                extracted_files.append(file_path_text)

            if collection == "BILLS" and format == "mods":
                # When we download bill files, also create the text-versions/data.json file
                # which extracts commonly used components of the MODS XML, whenever we update
                # that MODS file.
                extract_bill_version_metadata(package_name, os.path.dirname(package_file))

    return extracted_files


def get_bill_id_for_package(package_name, with_version=True, restrict_to_congress=None):
    m = re.match(r"(\d+)([a-z]+)(\d+)([a-z][a-z0-9]*|)$", package_name)
    if not m:
        raise Exception("Unmatched bill document package name: " + package_name)
    congress, bill_type, bill_number, version_code = m.groups()

    if restrict_to_congress and int(congress) != int(restrict_to_congress):
        return None

    if not with_version:
        return ("%s%s-%s" % (bill_type, bill_number, congress), version_code)
    else:
        return "%s%s-%s-%s" % (bill_type, bill_number, congress, version_code)


def get_output_path(collection, package_name, options):
    # Where to store the document files?

    # The path will depend a bit on the collection.
    if collection == "BILLS":
        # Store with the other bill data ([congress]/bills/[billtype]/[billtype][billnumber]).
        bill_and_ver = get_bill_id_for_package(
            package_name,
            with_version=False,
            restrict_to_congress=options.get("congress"),
        )
        if not bill_and_ver:
            return None  # congress number does not match options["congress"]
        from core.tasks.bills import output_for_bill

        bill_id, version_code = bill_and_ver
        return output_for_bill(bill_id, "text-versions/" + version_code, is_data_dot=False)

    elif collection == "CRPT":
        # Store committee reports in [congress]/crpt/[reporttype].
        m = re.match(r"(\d+)([hse]rpt)(\d+)$", package_name)
        if not m:
            raise ValueError(package_name)
        congress, report_type, report_number = m.groups()
        if options.get("congress") and congress != options.get("congress"):
            return None  # congress number does not match options["congress"]
        return "%s/%s/%s/%s/%s" % (
            utils.data_dir(),
            congress,
            collection.lower(),
            report_type,
            report_type + report_number,
        )

    else:
        # Store in govinfo/COLLECTION/PKGNAME.
        path = "%s/govinfo/%s/%s" % (utils.data_dir(), collection, package_name)
        return path


def unwrap_text_in_html(data):
    text_content = str(html.fromstring(data).text_content())
    return text_content.encode("utf8")


# Downloading bulk data files


def mirror_bulkdata_file(collection, url, item_path, lastmod, options):
    # Return a list of files we downloaded.
    results = []

    # Where should we store the file?
    path = "%s/govinfo/%s/%s" % (utils.data_dir(), collection, item_path)

    # For BILLSTATUS, store this along with where we store the rest of bill
    # status data.
    if collection == "BILLSTATUS":
        from core.tasks.bills import output_for_bill

        bill_id, version_code = get_bill_id_for_package(
            os.path.splitext(os.path.basename(item_path.replace("BILLSTATUS-", "")))[0],
            with_version=False,
        )
        path = output_for_bill(bill_id, FDSYS_BILLSTATUS_FILENAME, is_data_dot=False)

    # Where should we store the lastmod found in the sitemap so that
    # we can tell later if the file has changed?
    lastmod_cache_file = os.path.splitext(path)[0] + "-lastmod.txt"

    # Do we already have this file up to date?
    if os.path.exists(lastmod_cache_file) and not options.get("force", False):
        if lastmod == utils.read(lastmod_cache_file):
            results.append(path)
            return results

    # With --cached, skip if the file is already downloaded.
    if os.path.exists(path) and options.get("cached", False):
        return

    # Download.
    logger.info(f'Downloading: "{path}".')
    data = utils.download(
        url,
        path,
        utils.merge(
            options,
            {
                'binary': True,
                'force': True,  # decision to cache was made above
                'to_cache': False,
            },
        ),
    )
    results.append(path)

    if not data:
        # Something failed.
        return

    # Write the current last modified date back to disk so we know the next time whether
    # we need to fetch the file again.
    utils.write(lastmod, lastmod_cache_file)

    return results


def extract_bill_version_metadata(package_name, text_path):
    bill_version_id = get_bill_id_for_package(package_name)

    bill_type, number, congress, version_code = utils.split_bill_version_id(bill_version_id)

    bill_version = {
        'bill_version_id': bill_version_id,
        'version_code': version_code,
        'urls': {},
    }

    mods_ns = {"mods": "http://www.loc.gov/mods/v3"}
    doc = etree.parse(os.path.join(text_path, "mods.xml"))
    locations = doc.xpath("//mods:location/mods:url", namespaces=mods_ns)

    for location in locations:
        label = location.attrib['displayLabel']
        if "HTML" in label:
            format = "html"
        elif "PDF" in label:
            format = "pdf"
        elif "XML" in label:
            format = "xml"
        else:
            format = "unknown"
        bill_version["urls"][format] = location.text

    bill_version["issued_on"] = doc.xpath("string(//mods:dateIssued)", namespaces=mods_ns)

    utils.write(
        json.dumps(bill_version, sort_keys=True, indent=2, default=utils.format_datetime),
        output_for_bill_version(bill_version_id),
    )


def output_for_bill_version(bill_version_id):
    bill_type, number, congress, version_code = utils.split_bill_version_id(bill_version_id)
    return "%s/%s/bills/%s/%s%s/text-versions/%s/data.json" % (
        utils.data_dir(),
        congress,
        bill_type,
        bill_type,
        number,
        version_code,
    )
