from datetime import date, timedelta
from dateutil import parser as dateparser
import json
from operator import itemgetter
import sys

import arrow
import redis
from requests import get

from flask import render_template, abort, request, Response, jsonify, url_for, redirect, Blueprint
from flask.ext.cache import Cache
from flask.ext.paginate import Pagination
from flask_login import login_required, current_user
from flask_mail import Message
from webargs.flaskparser import FlaskParser

from ..auth.views import generate_auth_header
from .. import app, mail
from .arguments import search_args
from .canned_text import EMAIL_RESPONSE
from .forms import ContactForm, SearchForm, NumSeries
from .utils import (pull_feed, create_display_links, getbrowsecontent,
                   SearchPublications, change_to_pubs_test,
                   munge_pubdata_for_display, extract_related_pub_info, jsonify_geojson)


# set UTF-8 to be default throughout app
reload(sys)
sys.setdefaultencoding("utf-8")

pubswh = Blueprint('pubswh', __name__,
                   template_folder='templates',
                   static_folder='static',
                   static_url_path='/pubswh/static')
pub_url = app.config['PUB_URL']
lookup_url = app.config['LOOKUP_URL']
supersedes_url = app.config['SUPERSEDES_URL']
browse_url = app.config['BROWSE_URL']
search_url = app.config['BASE_SEARCH_URL']
citation_url = app.config['BASE_CITATION_URL']
browse_replace = app.config['BROWSE_REPLACE']
contact_recipients = app.config['CONTACT_RECIPIENTS']
replace_pubs_with_pubs_test = app.config.get('REPLACE_PUBS_WITH_PUBS_TEST')
robots_welcome = app.config.get('ROBOTS_WELCOME')
json_ld_id_base_url = app.config.get('JSON_LD_ID_BASE_URL')
google_webmaster_tools_code = app.config.get('GOOGLE_WEBMASTER_TOOLS_CODE')
auth_endpoint_url = app.config.get('AUTH_ENDPOINT_URL')
preview_endpoint_url = app.config.get('PREVIEW_ENDPOINT_URL')
max_age = app.config["REMEMBER_COOKIE_DURATION"].total_seconds()
login_page_path = app.config['LOGIN_PAGE_PATH']
cache_config = app.config['CACHE_CONFIG']
redis_config = app.config['REDIS_CONFIG']


# should requests verify the certificates for ssl connections
verify_cert = app.config['VERIFY_CERT']


cache = Cache(app, config=cache_config)

cache.init_app(app)

def make_cache_key(*args, **kwargs):
    path = request.path
    args = str(hash(frozenset(request.args.items())))
    return (path + args).encode('utf-8')


@pubswh.errorhandler(404)
def page_not_found(e):
    return render_template('pubswh/404.html'), 404



@pubswh.route("/preview/<index_id>")
@login_required
def restricted_page(index_id):
    """
    web page which is restricted and requires the user to be logged in.
    """

    # generate the auth header from the request
    auth_header = generate_auth_header(request)
    # build the url to call the endpoint
    published_status = get(pub_url + 'publication/' + index_id,
                           params={'mimetype': 'json'}, verify=verify_cert).status_code
    # go out to manage and get the record
    response = get(preview_endpoint_url+index_id+'/preview', headers=auth_header, verify=verify_cert,
                   params={'mimetype': 'json'})
    print "preview status code: ", response.status_code
    if response.status_code == 200:
        record = response.json()
        pubdata = munge_pubdata_for_display(record, replace_pubs_with_pubs_test, supersedes_url, json_ld_id_base_url)
        related_pubs = extract_related_pub_info(pubdata)
        return render_template("pubswh/preview.html", indexID=index_id, pubdata=pubdata, related_pubs=related_pubs)
    # if the publication has been published (so it is out of manage) redirect to the right URL
    elif response.status_code == 404 and published_status == 200:
        return redirect(url_for('pubswh.publication', index_id=index_id))
    elif response.status_code == 404 and published_status == 404:
        return render_template('pubswh/404.html'), 404


@pubswh.route('/robots.txt')
def robots():
    return render_template('pubswh/robots.txt', robots_welcome=robots_welcome)

@pubswh.route('/opensearch.xml')
def open_search():
    return render_template('pubswh/opensearch.xml')


@pubswh.route('/' + google_webmaster_tools_code + '.html')
def webmaster_tools_verification():
    return render_template('pubswh/google_site_verification.html')


@pubswh.route('/')
@cache.cached(timeout=300, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def index():
    user = current_user.get_id()

    sp = SearchPublications(search_url)
    recent_publications_resp = sp.get_pubs_search_results(params={'pub_x_days': 7,
                                                                  'page_size': 6})  # bring back recent publications

    recent_pubs_content = recent_publications_resp[0]
    try:
        pubs_records = recent_pubs_content['records']
        for record in pubs_records:
            record = create_display_links(record)
            if replace_pubs_with_pubs_test:
                record['displayLinks']['Thumbnail'][0]['url'] = change_to_pubs_test(
                    record['displayLinks']['Thumbnail'][0]['url'])

    except TypeError:
        pubs_records = []  # return an empty list recent_pubs_content is None (e.g. the service is down)
    form = SearchForm(request.args)
    return render_template('pubswh/home.html',
                           recent_publications=pubs_records,
                           form=form)

# contact form
@pubswh.route('/contact', methods=['GET', 'POST'])
def contact():
    contact_form = ContactForm()
    if request.method == 'POST':
        if contact_form.validate_on_submit():
            human_name = contact_form.name.data
            human_email = contact_form.email.data
            if human_name:
                sender_str = '({name}, {email})'.format(name=human_name, email=human_email)
            else:
                sender_str = '({email})'.format(email=human_email)
            subject_line = 'Pubs Warehouse User Comments'  # this is want Remedy filters on to determine if an email
            # goes to the pubs support group
            message_body = contact_form.message.data
            message_content = EMAIL_RESPONSE.format(contact_str=sender_str, message_body=message_body)
            msg = Message(subject=subject_line,
                          sender=(human_name, human_email),
                          reply_to=('PUBSV2_NO_REPLY', 'pubsv2_no_reply@usgs.gov'),
                          # this is not what Remedy filters on to determine if a message
                          # goes to the pubs support group...
                          recipients=contact_recipients,
                          # will go to servicedesk@usgs.gov if application has DEBUG = False
                          body=message_content)
            mail.send(msg)
            return redirect(url_for(
                'pubswh.contact_confirmation'))  # redirect to a conf page after successful validation and message sending
        else:
            return render_template('pubswh/contact.html',
                                   contact_form=contact_form)  # redisplay the form with errors if validation fails
    elif request.method == 'GET':
        return render_template('pubswh/contact.html', contact_form=contact_form)


@pubswh.route('/contact_confirm')
def contact_confirmation():
    confirmation_message = 'Thank you for contacting the USGS Publications Warehouse support team.'
    return render_template('pubswh/contact_confirm.html', confirm_message=confirmation_message)


# leads to rendered html for publication page
@pubswh.route('/publication/<index_id>')
@cache.cached(timeout=600, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def publication(index_id):
    r = get(pub_url + 'publication/' + index_id, params={'mimetype': 'json'}, verify=verify_cert)
    if r.status_code == 404:
        return render_template('pubswh/404.html'), 404
    pubreturn = r.json()
    pubdata = munge_pubdata_for_display(pubreturn, replace_pubs_with_pubs_test, supersedes_url, json_ld_id_base_url)
    related_pubs = extract_related_pub_info(pubdata)
    if 'mimetype' in request.args and request.args.get("mimetype") == 'json':
        return jsonify(pubdata)
    if 'mimetype' in request.args and request.args.get("mimetype") == 'ris':
        content =  render_template('pubswh/ris_single.ris', result=pubdata)
        return Response(content, mimetype="application/x-research-info-systems",
                               headers={"Content-Disposition":"attachment;filename=USGS_PW_"+pubdata['indexId']+".ris"})
    else:
        return render_template('pubswh/publication.html',
                               indexID=index_id, 
                               pubdata=pubdata,
                               related_pubs=related_pubs
                               )

#clears the cache for a specific page
@pubswh.route('/clear_cache/', defaults={'path': ''})
@pubswh.route('/clear_cache/<path:path>')
def clear_cache(path):
    if cache_config['CACHE_TYPE'] == 'redis':
        args = str(hash(frozenset(request.args.items())))
        key = cache_config['CACHE_KEY_PREFIX']+'/'+(path + args).encode('utf-8')
        r = redis.StrictRedis(host=redis_config['host'], port=redis_config['port'], db=redis_config['db'])
        r.delete(key)
        return 'cache cleared '+path + " args: "+ str(request.args)
    else:
        cache.clear()
        return "no redis cache, full cache cleared"

@pubswh.route('/clear_full_cache/')
def clear_full_cache():
    cache.clear()
    return 'cache cleared'

# leads to json for selected endpoints
@pubswh.route('/lookup/<endpoint>')
def lookup(endpoint):
    endpoint_list = ['costcenters', 'publicationtypes', 'publicationsubtypes', 'publicationseries']
    endpoint = endpoint.lower()
    if endpoint in endpoint_list:
        r = get(lookup_url + endpoint, params={'mimetype': 'json'}, verify=verify_cert).json()
        return Response(json.dumps(r), mimetype='application/json')
    else:
        abort(404)


@pubswh.route('/documentation/faq')
@cache.cached(timeout=600, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def faq():
    app.logger.info('The FAQ function is being called')
    feed_url = 'https://my.usgs.gov/confluence//createrssfeed.action?types=page&spaces=pubswarehouseinfo&title=Pubs+Other+Resources&labelString=pw_faq&excludedSpaceKeys%3D&sort=modified&maxResults=10&timeSpan=3600&showContent=true&confirm=Create+RSS+Feed'
    return render_template('pubswh/faq.html', faq_content=pull_feed(feed_url))


@pubswh.route('/documentation/usgs_series')
@cache.cached(timeout=600, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def usgs_series():
    app.logger.info('The USGS Series function is being called')
    feed_url = 'https://my.usgs.gov/confluence//createrssfeed.action?types=page&spaces=pubswarehouseinfo&title=USGS+Series+Definitions&labelString=usgs_series&excludedSpaceKeys%3D&sort=modified&maxResults=10&timeSpan=3600&showContent=true&confirm=Create+RSS+Feed'
    return render_template('pubswh/usgs_series.html', usgs_series_content=pull_feed(feed_url))


@pubswh.route('/documentation/web_service_documentation')
@cache.cached(timeout=600, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def web_service_docs():
    app.logger.info('The web_service_docs function is being called')
    feed_url = 'https://my.usgs.gov/confluence/createrssfeed.action?types=page&spaces=pubswarehouseinfo&title=Pubs+Other+Resources&labelString=pubs_webservice_docs&excludedSpaceKeys%3D&sort=modified&maxResults=10&timeSpan=3600&showContent=true&confirm=Create+RSS+Feed'
    return render_template('pubswh/webservice_docs.html', web_service_docs=pull_feed(feed_url))


@pubswh.route('/documentation/other_resources')
@cache.cached(timeout=600, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def other_resources():
    app.logger.info('The other_resources function is being called')
    feed_url = 'https://my.usgs.gov/confluence/createrssfeed.action?types=page&spaces=pubswarehouseinfo&title=Pubs+Other+Resources&labelString=other_resources&excludedSpaceKeys%3D&sort=modified&maxResults=10&timeSpan=3600&showContent=true&confirm=Create+RSS+Feed'
    return render_template('pubswh/other_resources.html', other_resources=pull_feed(feed_url))


@pubswh.route('/browse/', defaults={'path': ''})
@pubswh.route('/browse/<path:path>')
@cache.cached(timeout=600, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def browse(path):
    app.logger.info("path: " + path)
    browsecontent = getbrowsecontent(browse_url + path, browse_replace)
    return render_template('pubswh/browse.html', browsecontent=browsecontent)


# this takes advantage of the webargs package, which allows for multiple parameter entries. e.g. year=1981&year=1976
@pubswh.route('/search', methods=['GET'])
@cache.cached(timeout=20, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def search_results():
    form = SearchForm(request.args)

    parser = FlaskParser()
    search_kwargs = parser.parse(search_args, request)
    if search_kwargs.get('page_size') is None or search_kwargs.get('page_size') == '':
        search_kwargs['page_size'] = '25'
    if search_kwargs.get('page') is None or search_kwargs.get('page') == '':
        search_kwargs['page'] = '1'
    if (search_kwargs.get('page_number') is None or search_kwargs.get('page_number') == '') \
            and search_kwargs.get('page') is not None:
        search_kwargs['page_number'] = search_kwargs['page']


    sp = SearchPublications(search_url)
    search_results_response, resp_status_code = sp.get_pubs_search_results(
        params=search_kwargs)  # go out to the pubs API and get the search results
    try:
        search_result_records = search_results_response['records']
        record_count = search_results_response['recordCount']
        pagination = Pagination(page=int(search_kwargs['page_number']), total=record_count,
                                per_page=int(search_kwargs['page_size']), record_name='Search Results', bs_version=3)
        search_service_down = None
        start_plus_size = int(search_results_response['pageRowStart']) + int(search_results_response['pageSize'])
        if record_count < start_plus_size:
            record_max = record_count
        else:
            record_max = start_plus_size

        result_summary = {'record_count': record_count, 'page_number': search_results_response['pageNumber'],
                          'records_per_page': search_results_response['pageSize'],
                          'record_min': (int(search_results_response['pageRowStart']) + 1), 'record_max': record_max}
    except TypeError:
        search_result_records = None
        pagination = None
        search_service_down = 'The backend services appear to be down with a {0} status.'.format(resp_status_code)
        result_summary = {}
    if 'mimetype' in request.args and request.args.get("mimetype") == 'ris':
        content = render_template('pubswh/ris_output.ris', search_result_records=search_result_records)
        return Response(content, mimetype="application/x-research-info-systems",
                               headers={"Content-Disposition":"attachment;filename=PubsWarehouseResults.ris"})
    if request.args.get('map') == 'True':
        for record in search_result_records:
            record = jsonify_geojson(record)

    return render_template('pubswh/search_results.html',
                           result_summary=result_summary,
                           search_result_records=search_result_records,
                           pagination=pagination,
                           search_service_down=search_service_down,
                           form=form, pub_url=pub_url)


@pubswh.route('/site-map')
def site_map():
    """
    View for troubleshooting application URL rules
    """
    app_urls = []

    for url_rule in app.url_map.iter_rules():
        app_urls.append((str(url_rule), str(url_rule.endpoint)))

    return render_template('pubswh/site_map.html', app_urls=app_urls)


@pubswh.route('/newpubs', methods=['GET'])
@cache.cached(timeout=60, key_prefix=make_cache_key, unless=lambda: current_user.is_authenticated())
def new_pubs():
    num_form = NumSeries()
    sp = SearchPublications(search_url)
    search_kwargs = {'pub_x_days': 30, "page_size": 500}  # bring back recent publications

    # Search if num_series subtype was checked in form
    if request.args.get('num_series') == 'y':
        num_form.num_series.data = True
        search_kwargs['subtypeName'] = 'USGS Numbered Series'

    # Handles dates from form. Searches back to date selected or defaults to past 30 days.
    if request.args.get('date_range'):
        time_diff = date.today() - dateparser.parse(request.args.get('date_range')).date()
        day_diff = time_diff.days
        if not day_diff > 0:
            num_form.date_range.data = date.today() - timedelta(30)
            search_kwargs['pub_x_days'] = 30
        else:
            num_form.date_range.data = dateparser.parse(request.args.get('date_range'))
            search_kwargs['pub_x_days'] = day_diff
    else:
        num_form.date_range.data = date.today() - timedelta(30)

    recent_publications_resp = sp.get_pubs_search_results(params=search_kwargs)
    recent_pubs_content = recent_publications_resp[0]

    try:
        pubs_records = recent_pubs_content['records']
        pubs_records.sort(key=itemgetter('displayToPublicDate'), reverse=True)
        for record in pubs_records:
            record['FormattedDisplayToPublicDate'] = \
                arrow.get(record['displayToPublicDate']).format('MMMM DD, YYYY HH:mm')
    except TypeError:
        pubs_records = []  # return an empty list recent_pubs_content is None (e.g. the service is down)

    return render_template('pubswh/new_pubs.html',
                           new_pubs=pubs_records,
                           num_form=num_form)


@pubswh.route('/legacysearch/search:advance/page=1/series_cd=<series_code>/year=<pub_year>/report_number=<report_number>')
@pubswh.route('/legacysearch/search:advance/page=1/series_cd=<series_code>/report_number=<report_number>')
def legacy_search(series_code=None, report_number=None, pub_year=None):
    """
    This is a function to deal with the fact that the USGS store has dumb links to the warehouse
    based on the legacy search, which had all the query params in a backslash-delimited group.  A couple lines of
    javascript on the index page (see the bottom script block on the index page) passes the legacy query string to this
    function, and then this function reinterprets the string and redirects to the new search.

    :param series_code: the series code, which we will have to map to series name
    :param pub_year: the publication year, two digit, so we will have to make a guess as to what century they want
    :param report_number: report number- we can generally just pass this through
    :return: redirect to new search page with legacy arguments mapped to new arguments
    """
    # all the pubcodes that might be coming from the USGS store
    usgs_series_codes = {'AR': 'Annual Report', 'A': 'Antarctic Map', 'B': 'Bulletin', 'CIR': 'Circular',
                         'CP': 'Circum-Pacific Map', 'COAL': 'Coal Map', 'DS': 'Data Series', 'FS': 'Fact Sheet',
                         'GF': 'Folios of the Geologic Atlas', 'GIP': 'General Information Product',
                         'GQ': 'Geologic Quadrangle', 'GP': 'Geophysical Investigation Map', 'HA': 'Hydrologic Atlas',
                         'HU': 'Hydrologic Unit', 'I': 'IMAP', 'L': 'Land Use/ Land Cover',
                         'MINERAL': 'Mineral Commodities Summaries', 'MR': 'Mineral Investigations Resource Map',
                         'MF': 'Miscellaneous Field Studies Map', 'MB': 'Missouri Basin Study', 'M': 'Monograph',
                         'OC': 'Oil and Gas Investigation Chart', 'OM': 'Oil and Gas Investigation Map',
                         'OFR': 'Open-File Report', 'PP': 'Professional Paper', 'RP': 'Resource Publication',
                         'SIM': 'Scientific Investigations Map', 'SIR': 'Scientific Investigations Report',
                         'TM': 'Techniques and Methods', 'TWRI': 'Techniques of Water-Resource Investigation',
                         'TEI': 'Trace Elements Investigations', 'TEM': 'Trace Elements Memorandum',
                         'WDR': 'Water Data Report', 'WSP': 'Water Supply Paper',
                         'WRI': 'Water-Resources Investigations Report'}

    # horrible hack to deal with the fact that the USGS store apparently never heard of 4 digit dates

    if pub_year is not None:
        if 30 <= int(pub_year) < 100:
            pub_year = ''.join(['19', pub_year])
        elif int(pub_year) < 30:
            pub_year = ''.join(['20', pub_year])
        return redirect(url_for('pubswh.search_results', q=series_code+" "+report_number, year=pub_year, advanced=True))

    return redirect(url_for('pubswh.search_results', q=series_code+" "+report_number))



@pubswh.route('/unapi')
def unapi():
    """
    this is an unapi format, which appears to be the only way to get a good export to zotero that has all the Zotero fields
    Documented here: http://unapi.info/specs/
    :return: rendered template of (at this time) bibontology rdf, which maps directly to Zotero Fields
    """

    formats = {'rdf_bibliontology': {'type': 'application/xml', 'docs': "http://bibliontology.com/specification",
                                     'template': 'rdf_bibliontology.rdf'}}
    unapi_id = request.args.get('id')
    unapi_format = request.args.get('format')
    if unapi_format is None or unapi_format not in formats:
        if unapi_id is not None:
            unapi_id = unapi_id.split('/')[-1]
        return render_template('pubswh/unapi_formats.xml', unapi_id=unapi_id, formats=formats,  mimetype='text/xml')
    if unapi_id is not None and unapi_format in formats:
        unapi_id = unapi_id.split('/')[-1]
        r = get(pub_url + 'publication/' + unapi_id, params={'mimetype': 'json'}, verify=verify_cert)
        if r.status_code == 404:
            return render_template('pubswh/404.html'), 404
        pubdata = r.json()
        return render_template('pubswh/'+formats[unapi_format]['template'], pubdata=pubdata, formats=formats,  mimetype='text/xml')

