"""Functions for processing the xml associated with the Federal Register's
notices"""
from collections import namedtuple
from datetime import date, datetime
import logging
import os
from urlparse import urlparse

from cached_property import cached_property
from lxml import etree
import requests

from regparser import regs_gov
from regparser.grammar.unified import notice_cfr_p
from regparser.history.delays import delays_in_sentence
from regparser.index import xml_sync
from regparser.notice.amendments import fetch_amendments
from regparser.notice.dates import fetch_dates
from regparser.tree.xml_parser.xml_wrapper import XMLWrapper
import settings

logger = logging.getLogger(__name__)

TitlePartsRef = namedtuple("TitlePartsRef", ["title", "parts"])


def add_children(el, children):
    """
    Given an element and a list of children, recursively appends
    children as EREGS_SUBAGENCY elements with the appropriate
    attributes, and appends their children to them, etc.

    :arg Element el:    The XML element to add child elements to.
                        Should be either EREGS_AGENCY or
                        EREGS_SUBAGENCY.
    :arg list children: dict objects containing the agency information.
                        Must have subagencies in `children` fields.

    :rtype: XML Element
    """
    for agency in children:
        sub_el = etree.Element("EREGS_SUBAGENCY", **{
            "name": str(agency["name"]),
            "raw-name": str(agency["raw_name"]),
            "agency-id": str(agency["id"])
        })
        add_children(sub_el, agency["children"])
        el.append(sub_el)
    return el


class NoticeXML(XMLWrapper):
    """Wrapper around a notice XML which provides quick access to the XML's
    encoded data fields"""
    def delays(self):
        """Pull out FRDelays found in the DATES tag"""
        dates_str = "".join(p.text for p in self.xpath(
            "(//DATES/P)|(//EFFDATE/P)"))
        return [delay for sent in dates_str.split('.')
                for delay in delays_in_sentence(sent)]

    def _set_date_attr(self, date_type, value):
        """Modify the XML tree so that it contains meta data for a date
        field. Accepts both strings and dates"""
        dates_tag = self.xpath('//DATES')
        if dates_tag:
            dates_tag = dates_tag[0]
        else:   # Tag wasn't present; create it
            dates_tag = etree.Element("DATES")
            self.xml.insert(0, dates_tag)
        if isinstance(value, date):
            value = value.isoformat()
        if value is None:
            value = ''
        dates_tag.attrib["eregs-{}-date".format(date_type)] = value

    def derive_rins(self):
        """Extract regulatory id numbers from the XML (in the RINs tag)"""
        xml_rins = self.xpath('//RIN')
        for xml_rin in xml_rins:
            rin = xml_rin.text.replace("RIN", "").strip()
            yield rin

    def derive_docket_ids(self):
        """Extract docket numbers from the XML (in the DEPDOC tag)"""
        docket_ids = []
        xml_did_els = self.xpath('//DEPDOC')
        for xml_did_el in xml_did_els:
            did_str = xml_did_el.text.replace("[", "").replace("]", "")
            docket_ids.extend([d.strip() for d in did_str.split(";")])
        return docket_ids

    def set_agencies(self, agencies=None):
        """
        SIDE EFFECTS: this operates on the XML of the NoticeXML itself as well
        as returning some information.

        Adds elements to the NoticeXML to reflect information about the
        agencies connected to to notice.

        Looks for that information in a list of dicts passed in as
        ``agencies``, then adds it to the beginning of the XML as a set of
        elements that will look something like this::


            <EREGS_AGENCIES>
                <EREGS_AGENCY name="x" agency-id="00" raw-name="X">
                    <EREGS_SUBAGENCY name="y" agency-id="01" raw-name="Y">
                    </EREGS_SUBAGENCY>
                </EREGS_AGENCY>
            </EREGS_AGENCIES>

        :arg list agencies: dict objects containing agency information,
                            including ``id``, ``parent_id``, ``name``, and
                            ``raw_name``.

        :rtype: dict
        :returns:   A dict of ``id``: ``defaultdict``, where the id is
                    the id of the agency, and the ``defaultdicts`` are nested
                    to reflect their parent/child relationships.
        """

        if not agencies:
            # The FR Notice XML doesn't tend to have all the metadata we need
            # contained within it, so don't try to parse that, just log an
            # error.
            logging.warn("Preprocessing notice: no agency metadata.")
            return {}

        # We need turn turn the references to parent_ids into a tree of dicts
        # that contain subagencies in children fields:
        for agency in agencies:
            agency["children"] = []
        agency_map = {agency["id"]: agency for agency in agencies}
        child_keys = []
        for key in agency_map:
            agency = agency_map[key]
            if agency.get("parent_id") and agency["parent_id"] in agency_map:
                agency_map[agency["parent_id"]]["children"].append(agency)
                child_keys.append(key)
        for key in child_keys:
            del agency_map[key]

        # Add the elements, starting with a parent ``EREGS_AGENCIES`` element.
        agencies_el = etree.Element("EREGS_AGENCIES")
        for agency_id in agency_map:
            agency = agency_map[agency_id]
            has_parent = agency.get("parent_id")
            tag = "EREGS_SUBAGENCY" if has_parent else "EREGS_AGENCY"
            agency_el = etree.Element(tag, **{
                "name": str(agency["name"]),
                "raw-name": str(agency["raw_name"]),
                "agency-id": str(agency["id"])
            })
            add_children(agency_el, agency.get("children", []))
            agencies_el.append(agency_el)

        self.xml.insert(0, agencies_el)
        return agency_map

    def derive_cfr_refs(self):
        """Pull out CFR information from the CFR tag"""
        for cfr_elm in self.xpath('//CFR'):
            result = notice_cfr_p.parseString(cfr_elm.text)
            yield TitlePartsRef(result.cfr_title, list(result.cfr_parts))

    def derive_closing_date(self):
        """Attempt to parse comment closing date from DATES tags. Returns a
        datetime.date and sets the corresponding field"""
        dates = fetch_dates(self.xml) or {}
        if 'comments' in dates:
            comments = datetime.strptime(
                dates['comments'][0], "%Y-%m-%d").date()
            return comments

    def derive_effective_date(self):
        """Attempt to parse effective date from DATES tags. Returns a
        datetime.date and sets the corresponding field"""
        dates = fetch_dates(self.xml) or {}
        if 'effective' in dates:
            effective = datetime.strptime(
                dates['effective'][0], "%Y-%m-%d").date()
            return effective

    def _get_date_attr(self, date_type):
        """Pulls out the date set in `set_date_attr`, as a datetime.date. If
        not present, returns None"""
        value = self.xpath(".//DATES")[0].get('eregs-{}-date'.format(
            date_type))
        if value:
            return datetime.strptime(value, "%Y-%m-%d").date()

    def derive_where_needed(self):
        """A handful of fields might be parse-able from the original XML. If
        we don't have values through modification, derive them here"""
        if not self.comments_close_on:
            self.comments_close_on = self.derive_closing_date()
        if not self.rins:
            self.rins = self.derive_rins()
        if not self.cfr_refs:
            self.cfr_refs = self.derive_cfr_refs()
        if not self.effective:
            self.effective = self.derive_effective_date()
        if not self.comment_docket_id:
            for docket_id in self.docket_ids:
                proposal = regs_gov.proposal(docket_id, self.version_id)
                if proposal:
                    self.comment_docket_id = proposal.id
        if not self.supporting_documents:
            supporting = []
            for docket_id in self.docket_ids:
                supporting.extend(regs_gov.supporting_docs(docket_id))
            self.support_documents = supporting

    # --- Setters/Getters for specific fields. ---
    # We encode relevant information within the XML, but wish to provide easy
    # access

    @property
    def rins(self):
        return [_.attrib['rin'] for _ in self.xpath("//EREGS_RIN")]

    @rins.setter
    def rins(self, value):
        """
        Modify the XML tree so that it contains meta data for regulation id
        numbers.
        The Federal Register API implies that documents can have more than one.

        The XML we're adding will look something like this::

            <EREGS_RINS>
                <EREGS_RIN rin="2050-AG65" />
            </EREGS_RINS>

        :arg list value: RINs, which should be strings.
        """
        rins_el = self.xpath('//EREGS_RINS')
        if rins_el:
            rins_el = rins_el[0]
        else:   # Tag wasn't present; create it
            rins_el = etree.Element("EREGS_RINS")
        for rin in value:
            etree.SubElement(rins_el, "EREGS_RIN", rin=rin)
        self.xml.insert(0, rins_el)

    @property
    def docket_ids(self):
        return [_.attrib['docket_id'] for _ in self.xpath("//EREGS_DOCKET_ID")]

    @docket_ids.setter
    def docket_ids(self, value):
        """
        Modify the XML tree so that it contains meta data for docket ids.

        The XML we're adding will look something like this::

            <EREGS_DOCKET_IDS>
                <EREGS_DOCKET_ID docket_id="EPA-HQ-SFUND-2010-1086" />
                <EREGS_DOCKET_ID docket_id="FRL-9925-69-OLEM" />
            </EREGS_DOCKET_IDS>

        :arg list value: docket_ids, which should be strings.
        """
        dids_el = self.xpath('//EREGS_DOCKET_IDS')
        if dids_el:
            dids_el = dids_el[0]
        else:   # Tag wasn't present; create it
            dids_el = etree.Element("EREGS_DOCKET_IDS")
        for docket_id in value:
            etree.SubElement(dids_el, "EREGS_DOCKET_ID", docket_id=docket_id)
        self.xml.insert(0, dids_el)

    @property
    def cfr_refs(self):
        refs = []
        for title_el in self.xpath("//EREGS_CFR_TITLE_REF"):
            parts = title_el.xpath("EREGS_CFR_PART_REF")
            parts = [int(p.attrib["part"]) for p in parts]
            refs.append(TitlePartsRef(title=int(title_el.attrib["title"]),
                                      parts=parts))

        return refs

    @cfr_refs.setter
    def cfr_refs(self, value):
        """
        Transform the XML to include elements that look like this::

            <EREGS_CFR_REFS>
                <EREGS_CFR_TITLE_REF title="40">
                    <EREGS_CFR_PART_REF part="300" />
                    <EREGS_CFR_PART_REF part="310" />
                </EREGS_CFR_TITLE_REF>
            </EREGS_CFR_REFS>
        :arg list value: List of TitlePartsRef elements
        """
        refs_el = etree.Element("EREGS_CFR_REFS")
        for ref in value:
            el = etree.SubElement(refs_el, "EREGS_CFR_TITLE_REF",
                                  title=str(ref.title))
            for part in ref.parts:
                etree.SubElement(el, "EREGS_CFR_PART_REF", part=str(part))
        self.xml.insert(0, refs_el)

    @property
    def cfr_ref_pairs(self):
        return [(ref.title, part)
                for ref in self.cfr_refs for part in ref.parts]

    @property
    def comments_close_on(self):
        return self._get_date_attr('comments-close-on')

    @comments_close_on.setter
    def comments_close_on(self, value):
        self._set_date_attr('comments-close-on', value)

    @property
    def effective(self):
        return self._get_date_attr('effective')

    @effective.setter
    def effective(self, value):
        self._set_date_attr('effective', value)

    @property
    def published(self):
        return self._get_date_attr('published')

    @published.setter
    def published(self, value):
        self._set_date_attr('published', value)

    @property
    def fr_volume(self):
        value = self.xpath(".//PRTPAGE")[0].attrib.get('eregs-fr-volume')
        if value:
            return int(value)

    @fr_volume.setter
    def fr_volume(self, value):
        for prtpage in self.xpath(".//PRTPAGE"):
            prtpage.attrib['eregs-fr-volume'] = str(value)

    @property
    def start_page(self):
        return int(self.xpath(".//PRTPAGE")[0].attrib["P"]) - 1

    @property
    def end_page(self):
        return int(self.xpath(".//PRTPAGE")[-1].attrib["P"])

    @property
    def version_id(self):
        return self.xml.attrib.get('eregs-version-id')

    @version_id.setter
    def version_id(self, value):
        self.xml.attrib['eregs-version-id'] = str(value)

    @property
    def fr_html_url(self):
        return self.xml.attrib.get('fr-html-url')

    @fr_html_url.setter
    def fr_html_url(self, value):
        self.xml.attrib['fr-html-url'] = value

    @cached_property        # rather expensive operation, so cache results
    def amendments(self):
        return fetch_amendments(self.xml)

    @property
    def fr_citation(self):
        return '{} FR {}'.format(self.fr_volume, self.start_page)

    @property
    def title(self):
        return self.xpath('//SUBJECT')[0].text

    @property
    def primary_agency(self):
        return self.xpath('//AGENCY')[0].text

    @property
    def comment_docket_id(self):
        return self.xml.attrib.get('eregs-comment-docket-id')

    @comment_docket_id.setter
    def comment_docket_id(self, value):
        self.xml.attrib['eregs-comment-docket-id'] = str(value)

    @property
    def supporting_documents(self):
        """:rtype: list of regs_gov.RegsGovDoc"""
        attribs = [dict(s.attrib)
                   for s in self.xpath('//EREGS_SUPPORTING_DOC')]
        for attrib in attribs:
            attrib['fr_id'] = attrib['fr_id'] or None
        return [regs_gov.RegsGovDoc(**attrib) for attrib in attribs]

    @supporting_documents.setter
    def supporting_documents(self, value):
        """A docket consists of multiple, related documents. The most
        important is generally the proposal and/or final rule, but there are
        often supporting documents we need to link to.

        Modify the XML to look like::

            <EREGS_SUPPORTING_DOCS>
                <EREGS_SUPPORTING_DOC
                    regs_id="EPA-HQ-SFUND-2010-1086-0001"
                    href="http://example.com/0001"
                    fr_id="12345-6789"
                    title="Title goes here" />
                <EREGS_SUPPORTING_DOC
                    regs_id="EPA-HQ-SFUND-2010-1086-0002"
                    href="http://example.com/0002"
                    fr_id=""
                    title="Title goes here" />
            </EREGS_SUPPORTING_DOCS>

        :arg list value: list of regs_gov.RegsGovDocs
        """
        container = self.xpath('//EREGS_SUPPORTING_DOCS')
        if container:
            container = container[0]
        else:   # Tag wasn't present; create it
            container = etree.SubElement(self.xml, 'EREGS_SUPPORTING_DOCS')
        for doc in value:
            doc = {key: value or '' for key, value in doc._asdict().items()}
            etree.SubElement(container, 'EREGS_SUPPORTING_DOC', **doc)

    def as_dict(self):
        """We use JSON to represent notices in the API. This converts the
        relevant data into a dictionary to get one step closer. Unfortunately,
        that design assumes a single cfr_part"""
        cfr_ref = self.cfr_refs[0]
        notice = {'amendments': self.amendments,
                  'cfr_parts': [str(part) for part in cfr_ref.parts],
                  'cfr_title': cfr_ref.title,
                  'dockets': self.docket_ids,
                  'document_number': self.version_id,
                  'fr_citation': self.fr_citation,
                  'fr_url': self.fr_html_url,
                  'fr_volume': self.fr_volume,
                  # @todo - SxS depends on this; we should remove soon
                  'meta': {'start_page': self.start_page},
                  'primary_agency': self.primary_agency,
                  'publication_date': self.published.isoformat(),
                  'regulation_id_numbers': self.rins,
                  'supporting_documents': [
                      d._asdict() for d in self.supporting_documents],
                  'title': self.title}
        if self.comments_close_on:
            notice['comments_close'] = self.comments_close_on.isoformat()
        if self.effective:
            notice['effective_on'] = self.effective.isoformat()
        if self.comment_docket_id:
            notice['comment_docket_id'] = self.comment_docket_id
        return notice


def local_copies(url):
    """Use any local copies (potentially with modifications of the FR XML)"""
    parsed_url = urlparse(url)
    path = parsed_url.path.replace('/', os.sep)
    notice_dir_suffix, file_name = os.path.split(path)
    for xml_path in settings.LOCAL_XML_PATHS + [xml_sync.GIT_DIR]:
        if os.path.isfile(xml_path + path):
            return [xml_path + path]
        else:
            prefix = file_name.split('.')[0]
            notice_directory = xml_path + notice_dir_suffix
            notices = []
            if os.path.exists(notice_directory):
                notices = os.listdir(notice_directory)

            relevant_notices = [os.path.join(notice_directory, n)
                                for n in notices if n.startswith(prefix)]
            if relevant_notices:
                return relevant_notices
    return []


def notice_xmls_for_url(doc_num, notice_url):
    """Find, preprocess, and return the XML(s) associated with a particular FR
    notice url"""
    local_notices = local_copies(notice_url)
    if local_notices:
        logger.info("using local xml for %s", notice_url)
        for local_notice_file in local_notices:
            with open(local_notice_file, 'r') as f:
                yield NoticeXML(f.read(), local_notice_file).preprocess()
    else:
        logger.info("fetching notice xml for %s", notice_url)
        content = requests.get(notice_url).content
        yield NoticeXML(content, notice_url).preprocess()


def xmls_for_url(notice_url):
    # @todo: remove the need for this function
    return [notice_xml.xml
            for notice_xml in notice_xmls_for_url('N/A', notice_url)]
