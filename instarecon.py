#!/usr/bin/env python
import sys
import socket
import argparse
import requests
import re
import time
from random import randint
import csv
import os

import pythonwhois as whois #http://cryto.net/pythonwhois/usage.html https://github.com/joepie91/python-whois
from ipwhois import IPWhois as ipw #https://pypi.python.org/pypi/ipwhois
import ipaddress as ipa #https://docs.python.org/3/library/ipaddress.html
import dns.resolver,dns.reversename,dns.name #http://www.dnspython.org/docs/1.12.0/
import shodan #https://shodan.readthedocs.org/en/latest/index.html


class Host(object):
    '''
    Host represents an entity on the internet. Can originate from a domain or from an IP.

    Attributes are:

        domain - DNS/Domain name e.g. google.com
        
        type - either 'domain' or 'ip', depending on original information passed to __init__

        ips - List of instances of IP. Each instance contains:
            ip - Str representation of IP e.g. 8.8.8.8
            rev_domains - list of str representing reverse domains related to ip 
            whois_ip - Dict containing results from a Whois lookup on an IP
            shodan - Dict containing results from Shodan lookups
            cidr - Str of CIDR that this ip is part of (taken from whois_ip results)


        mx - Set of Hosts for each DNS MX entry for original self.domain
        
        ns - Set of Hosts for each DNS NS entry for original self.domain
        
        whois_domain - str representation of the Whois query
        
        subdomains - Set of Hosts for each related Host found that is a subdomain of self.domain
        
        linkedin_page - Str of LinkedIn url that contains domain in html
        
        cidrs - CIDRs related to this Host, taken from each ip in self.ips
        
        related_hosts - Set of Hosts that may or may not be related to self, not currently in use. 
                        Taken from reverse_dns_lookup_on_related_cidrs.

    '''

    def __init__(self,domain=None,ips=[],reverse_domains=[]):
        
        if ips and domain or domain: #target is domain, with or without ip already resolved by scan.add_host()
            self.type = 'domain'
        elif ips: #target is ip
            self.type = 'ip'
        else:
            raise ValueError

        self.domain = domain
        self.ips = [ IP(str(ip),reverse_domains) for ip in ips ]
        self.mx = set()
        self.ns = set()
        self.whois_domain = None
        self.subdomains = set()
        self.linkedin_page = None
        
        self.cidrs = {}

        self.related_hosts = set()

    def __str__(self):
        if self.type == 'domain':
            return str(self.domain)
        elif self.type == 'ip':
            return str(self.ips[0])
    
    def __hash__(self):
        if self.type == 'domain':
            return hash(('domain',self.domain))
        elif self.type == 'ip':
            return hash(('ip',','.join([str(ip) for ip in self.ips])))
    
    def __eq__(self,other):
        if self.type == 'domain':
            return self.domain == other.domain
        elif self.type == 'ip':
            return self.ips == other.ips

    def dns_lookups(self):
        '''
        Does basic DNS lookups on a host.

        1) Direct DNS lookup on self.domain
        2) Reverse DNS lookup on each self.ips
        '''

        if self.type == 'domain':
            self._get_ips()
            for ip in self.ips: ip.get_rev_domains() 

        if self.type == 'ip':
            for ip in self.ips: ip.get_rev_domains() 

        return self

    def mx_dns_lookup(self):
        '''
        DNS lookup to find MX entries i.e. mail servers responsible for self.domain
        '''
        try:
            if self.domain:
                mx_list = self._ret_mx_by_name(self.domain)
                self.mx.update([ Host(domain=mx).dns_lookups() for mx in mx_list ])
                self._add_to_subdomains_if_valid(subdomains_as_hosts=self.mx)

        except Exception as e:
            Scan.error('[-] MX lookup failed for '+self.domain,sys._getframe().f_code.co_name)

    def ns_dns_lookup(self):
        '''
        DNS lookup to find NS entries i.e. name/DNS servers responsible for self.domain
        '''        
        try:
            if self.domain:
                ns_list = self._ret_ns_by_name(self.domain)
                self.ns.update([ Host(domain=ns).dns_lookups() for ns in ns_list ])
                self._add_to_subdomains_if_valid(subdomains_as_hosts=self.ns)

        except Exception as e:
            Scan.error('[-] NS lookup failed for '+self.domain,sys._getframe().f_code.co_name)
    
    def google_lookups(self):
        '''
        Queries google to find related subdomains and linkedin pages.
        '''
        if self.domain:
            self.linkedin_page = self._ret_linkedin_page_from_google(self.domain)

            self._add_to_subdomains_if_valid(self._ret_subdomains_from_google())

    def get_rev_domains_for_ips(self):
        '''
        Reverse DNS lookup on each IP in self.ips
        '''
        if self.ips:
            for ip in self.ips:
                ip.get_rev_domains()
        return self

    def get_whois_domain(self,num=0):
        '''
        Whois lookup on self.domain. Saved in self.whois_domain as string, 
        since each top-level domain has a way of representing data.
        This makes it hard to index it, almost a project on its on.
        '''
        try:
            if self.domain:
                query = whois.get_whois(self.domain)
                
                if 'raw' in query:
                    self.whois_domain = query['raw'][0].lstrip().rstrip()

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
            pass
    
    def get_all_whois_ip(self):
        '''
        IP Whois lookups on each ip within self.ips
        Saved in each ip.whois_ip as dict, since this is how it is returned by ipwhois library.
        '''
        for ip in self.ips:

            cidr_found = False

            #Iterate cidrs related to Host to see if it already exists 
            for cidr,whois_ip in self.cidrs.iteritems():
                if ipa.ip_address(ip.ip.decode('unicode-escape')) in ipa.ip_network(cidr.decode('unicode-escape')):
                    #If cidr is already in Host, we won't get_whois_ip again.
                    #Instead we will save cidr in ip just to make it easier to relate them later
                    ip.cidr,ip.whois = cidr,whois_ip
                    cidr_found = True
                    break

            if not cidr_found:
                ip.get_whois_ip()
                self.cidrs[ip.cidr] = ip.whois_ip

    def get_all_shodan(self,key):
        '''
        Shodan lookups for each ip within self.ips.
        Saved in ip.shodan as dict.
        '''
        if key:
            for ip in self.ips:
                ip.get_shodan(key)
    
    def _get_ips(self):
        '''
        Does direct DNS lookup to get IPs from self.domains.
        Used internally by self.dns_lookups()
        '''
        if self.domain and not self.ips:
            ips = self._ret_host_by_name(self.domain)
            if ips:
                self.ips = [ IP(str(ip)) for ip in ips ]
        return self

    @staticmethod
    def _ret_host_by_name(name):
        try:
            return dns.resolver.query(name)
        except Exception as e:
            Scan.error('[-] Host lookup failed for '+name,sys._getframe().f_code.co_name)
            pass

    @staticmethod
    def _ret_mx_by_name(name):
        try:
            #rdata.exchange for domains and rdata.preference for integer
            return [str(mx.exchange).rstrip('.') for mx in dns.resolver.query(name,'MX')]
        except Exception as e:
            Scan.error('[-] MX lookup failed for '+name,sys._getframe().f_code.co_name)
            pass

    @staticmethod
    def _ret_ns_by_name(name):
        try:
            #rdata.exchange for domains and rdata.preference for integer
            return [str(ns).rstrip('.') for ns in dns.resolver.query(name,'NS')]
        except Exception as e:
            Scan.error('[-] NS lookup failed for '+name,sys._getframe().f_code.co_name)
            pass

    @staticmethod
    def _ret_linkedin_page_from_google(name):
        '''
        Uses a google query to find a possible LinkedIn page related to name (usually self.domain)

        Google query is "site:linkedin.com/company name", and first result is used
        '''
        try:
            request='http://google.com/search?hl=en&meta=&num=10&q=site:linkedin.com/company%20"'+name+'"'
            google_search = requests.get(request)
            google_results = re.findall('<cite>(.+?)<\/cite>', google_search.text)
            for url in google_results:
                if 'linkedin.com/company/' in url:
                    return re.sub('<.*?>', '', url)
        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)


    def _ret_subdomains_from_google(self):
        ''' 
        This method uses google dorks to get as many subdomains from google as possible

        It returns a set of Hosts for each subdomain found in google

        Each Host will have dns_lookups() already callled, with possibly ips and rev_domains filled
        '''

        def _google_subdomains_lookup(domain,subdomains_to_avoid,num,counter):
            '''
            Sub method that reaches out to google using the following query:
            site:*.domain -site:subdomain_to_avoid1 -site:subdomain_to_avoid2 -site:subdomain_to_avoid3...

            Returns list of unique subdomain strings
            '''

            #Sleep some time between 0 - 3.999 seconds
            time.sleep(randint(0,3)+randint(0,1000)*0.001)


            request = 'http://google.com/search?hl=en&meta=&num='+str(num)+'&start='+str(counter)+'&q='
            request = ''.join([request,'site%3A%2A',domain])

            for subdomain in subdomains_to_avoid:
                #Don't want to remove original name from google query
                if subdomain != domain:
                    request = ''.join([request,'%20%2Dsite%3A',str(subdomain)])

            google_search = None
            try:
                google_search = requests.get(request)
            except Exception as e:
                Scan.error(e,sys._getframe().f_code.co_name)
                                    
            new_subdomains = set()
            if google_search:
                google_results = re.findall('<cite>(.+?)<\/cite>', google_search.text)


                for url in set(google_results):
                    #Removing html tags from inside url (sometimes they ise <b> or <i> for ads)
                    url = re.sub('<.*?>', '', url)

                    #Follows Javascript pattern of accessing URLs
                    g_host = url
                    g_protocol = ''
                    g_pathname = ''

                    temp = url.split('://')

                    #If there is g_protocol e.g. http://, ftp://, etc
                    if len(temp)>1:
                        g_protocol = temp[0]
                        #remove g_protocol from url
                        url = ''.join(temp[1:])

                    temp = url.split('/')
                    #if there is a pathname after host
                    if len(temp)>1:

                        g_pathname = '/'.join(temp[1:])
                        g_host = temp[0]

                    new_subdomains.add(g_host)

                #TODO do something with g_pathname and g_protocol
                #Currently not using protocol or pathname for anything
            return list(new_subdomains)

        #Keeps subdomains found by _google_subdomains_lookup
        subdomains_discovered = []
        #Variable to check if there is any new result in the last iteration
        subdomains_in_last_iteration = -1

        while len(subdomains_discovered) > subdomains_in_last_iteration:
                                
            subdomains_in_last_iteration = len(subdomains_discovered)
            
            subdomains_discovered += _google_subdomains_lookup(self.domain,subdomains_discovered,100,0)
            subdomains_discovered = list(set(subdomains_discovered))

        subdomains_discovered += _google_subdomains_lookup(self.domain,subdomains_discovered,100,100)
        subdomains_discovered = list(set(subdomains_discovered))

        return subdomains_discovered


    def _add_to_subdomains_if_valid(self,subdomains_as_str=[],subdomains_as_hosts=[]):
        '''
        Will add Hosts from subdomains_as_str or subdomains_as_hosts to self.subdomain if indeed these hosts are subdomains
        '''
        if subdomains_as_str:
            self.subdomains.update(
                [ Host(domain=subdomain).dns_lookups() for subdomain in subdomains_as_str if self._is_parent_domain_of(subdomain) ]
            )

        elif subdomains_as_hosts:
            self.subdomains.update(
                [ subdomain for subdomain in subdomains_as_hosts if self._is_parent_domain_of(subdomain) ]
            )

    def _is_parent_domain_of(self,subdomain):
        '''
        Checks if subdomain is indeed a subdomain of self.domain
        In addition it filters out invalid dns names
        '''
        if isinstance(subdomain, str):
            try:
                return dns.name.from_text(subdomain).is_subdomain(dns.name.from_text(self.domain))
            except Exception as e:
                pass

        elif isinstance(subdomain, Host):
            if subdomain.domain:
                try:
                    return dns.name.from_text(subdomain.domain).is_subdomain(dns.name.from_text(self.domain))
                except Exception as e:
                    pass

            elif subdomain.ips[0].rev_domains:
                for rev in subdomain.ips[0].rev_domains:
                    try:
                        if dns.name.from_text(rev).is_subdomain(dns.name.from_text(self.domain)):
                            return True
                    except Exception as e:
                        pass                

        return False

    def reverse_dns_lookup_on_related_cidrs(self,feedback=False):
        '''
        Does reverse dns lookups in all cidrs discovered that are related to this host

        Will be used to check for subdomains found through reverse lookup
        '''

        domain = dns.name.from_text(self.domain)

        #Reverse DNS lookup on all self.cidrs
        for cidr in self.cidrs:
            #For each ip in network cidr
            for ip in ipa.ip_network(cidr.decode('unicode-escape')):
                
                reverse_lookup = None

                try:
                    reverse_lookup = dns.resolver.query(dns.reversename.from_address(str(ip)),'PTR')
                except Exception as e:
                    pass
                except KeyboardInterrupt:
                    if raw_input('[-] Sure you want to stop scanning '+str(cidr)+\
                        '? Program flow will continue normally. [y]/n:') in ['y','Y','']:
                        break
                    else:
                        try:
                            reverse_lookup = dns.resolver.query(dns.reversename.from_address(str(ip)),'PTR')
                        except Exception as e:
                            pass

                if reverse_lookup:
                    #Organizing reverse lookup results
                    reverse_domains = [ str(domain).rstrip('.') for domain in reverse_lookup ]
                    #Creating new host
                    new_host = Host(ips=[ip],reverse_domains=reverse_domains)

                    #Append host to current host self.related_hosts
                    self.related_hosts.add(new_host)

                    #Adds new_host to self.subdomains if new_host indeed is subdomain
                    self._add_to_subdomains_if_valid(subdomains_as_hosts=[new_host])

                    if feedback: print new_host.print_all_ips()


    def print_all_ips(self):
        if self.ips:
            return '\n'.join([ ip.print_ip() for ip in self.ips ]).rstrip()
        
    def print_subdomains(self):
        return self._print_domains(sorted(self.subdomains, key=lambda x: x.domain))

    @staticmethod
    def _print_domains(hosts):
        #Static method that prints a list of domains with its respective ips and rev_domains
        #domains should be a list of Hosts
        if hosts:
            ret = ''
            for host in hosts:

                ret = ''.join([ret,host.domain])
                
                p = host.print_all_ips()
                if p: ret = ''.join([ret,'\n\t',p.replace('\n','\n\t')])
                
                ret = ''.join([ret,'\n'])

            return ret.rstrip().lstrip()

    def print_all_ns(self):
        #Print all NS records
        return self._print_domains(self.ns)

    def print_all_mx(self):
        #Print all MS records
        return self._print_domains(self.mx)

    def print_all_whois_ip(self):
        #Prints whois_ip records related to all self.ips
        ret = [IP.print_whois_ip(whois_ip) for cidr,whois_ip in self.cidrs.iteritems() ]
        return '\n'.join(ret).lstrip().rstrip()

    def print_all_shodan(self):
        #Print all Shodan entries (one for each IP in self.ips)

        ret = [ ip.print_shodan() for ip in self.ips if ip.shodan ]
        return '\n'.join(ret).lstrip().rstrip()

    def print_as_csv_lines(self):
        #Generator that yields each IP within self.ips will be returned as a csv line, one at a time
        #one Host csv line at a time
        
        yield ['Target: '+self.domain]
        
        if self.ips:
            yield [
                    'Domain',
                    'IP',
                    'Reverse domains',
                    'NS',
                    'MX',
                    'Subdomains',
                    'Domain whois',
                    'IP whois',
                    'Shodan',
                    'LinkedIn page',
                ]

            for ip in self.ips:
                ret = [
                    self.domain,
                    str(ip),
                    '\n'.join(ip.rev_domains),
                    self.print_all_ns(),
                    self.print_all_mx(),
                    self.print_subdomains(),
                    self.whois_domain,
                    IP.print_whois_ip(ip.whois_ip),
                    ip.print_shodan(),
                    self.linkedin_page,
                ]
                yield ret

        if self.subdomains:
            yield ['\n']
            yield ['Subdomains for '+str(self.domain)]
            yield ['Domain','IP','Reverse domains']
            
            for sub in sorted(self.subdomains):
                for ip in sub.ips:
                    ret = [
                        sub.domain,
                        ip.ip,#','.join([ str(ip) for ip in sub.ips ]),
                        ','.join([ ip.rev_domains ]),#','.join(ip.rev_domains) for ip in sub.ips ]),
                    ]

                yield ret

        if self.related_hosts:
            yield ['\n']
            yield ['Hosts related to '+str(self.domain)]
            yield ['IP','Reverse domains']

            for sub in sorted(self.related_hosts):
                ret = [
                    ','.join([ str(ip) for ip in sub.ips ]),
                    ','.join([ ','.join(ip.rev_domains) for ip in sub.ips ]),
                ]

                yield ret

    def do_all_lookups(self, shodan_key=None):
        '''
        This method does all possible lookups for a Host.
        Not called by any Host or Scan function, only here for testing purposes.
        '''
        self.dns_lookups()
        self.ns_dns_lookup()
        self.mx_dns_lookup()
        self.get_whois_domain()
        self.get_all_whois_ip()
        if shodan_key:
            self.get_all_shodan(shodan_key)
        self.google_lookups()


class IP(Host):
    '''
        IP and information specific to it. Hosts contain multiple IPs,
        as a domain can resolve to multiple IPs.

        Attirbutes are:
            ip - Str representation of this ip e.g. '8.8.8.8'

            whois_ip - Dict containing results for Whois lookups against self.ip

            cidr - Str CIDR that contains self.ip (taken from whois_ip), e.g. 8.8.8.0/24

            rev_domains - List of str for each reverse domain for self.ip, found through reverse DNS lookups

            shodan - Dict containing Shodan results
    '''

    def __init__(self,ip,rev_domains=[]):
        self.ip = str(ip)
        self.rev_domains = rev_domains
        self.whois_ip = None
        self.cidr = None
        self.shodan = None

    def __str__(self):
        return str(self.ip)

    def __hash__(self):
        return hash(('ip',self.ip))
    
    def __eq__(self,other):
        return self.ip == other.ip

    @staticmethod
    def _ret_host_by_ip(ip):
        try:
            return dns.resolver.query(dns.reversename.from_address(ip),'PTR')
        except Exception as e:
            Scan.error('[-] Host lookup failed for '+ip,sys._getframe().f_code.co_name)

    def get_rev_domains(self):
        try:
            rev_domains = None
            rev_domains = self._ret_host_by_ip(self.ip)
        
            if rev_domains:
                self.rev_domains = [ str(domain).rstrip('.') for domain in rev_domains ]

            return self

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
            pass

    def get_shodan(self, key):
        try:
            shodan_api_key = key
            api = shodan.Shodan(shodan_api_key)
            self.shodan = api.host(str(self))
        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)

    def get_whois_ip(self):

        raw_result = None
        try:
            raw_result = ipw(str(self)).lookup() or None
        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
        
        if raw_result:
            result = {}

            if raw_result['nets']:
                #get results from parent 'nets'
                for index,net in enumerate(raw_result['nets']):
                    for key,val in net.iteritems():
                        if val:
                            result['net'+str(index)+'_'+key] = net[key].replace('\n',', ') \
                                if key in net and net[key] else None

            #get all remaining results not in 'nets'
            for key,val in raw_result.iteritems():
                if key not in ['nets','query']:
                    if type(val) in [dict]:

                        new_dict = {}
                        
                        for key2, val2 in value.iteritems():
                            if val2 != None:
                                new_dict[key2] = val2
                                result_is_valid = True

                        result[key] = new_dict

                    else:
                        if raw_result[key]:
                            result[key] = str(raw_result[key]).replace('\n',', ')
                            result_is_valid = True

            self.whois_ip = result
            
            if self.whois_ip:
                if 'net0_cidr' in self.whois_ip:
                    self.cidr = self.whois_ip['net0_cidr']

            return self
    
    def print_ip(self):
        ret = str(self.ip)

        if self.rev_domains:
            if len(self.rev_domains) < 2:
                ret =  ''.join([ret,' - ',self.rev_domains[0]])
            else:
                for rev in self.rev_domains:
                    ret =  '\t'.join([ret,'\n',rev])

        return ret

    @staticmethod
    def print_whois_ip(whois_ip):
        try:
            if whois_ip:
                result = ''

                for key,val in sorted(whois_ip.iteritems()):
                    if val:
                        
                        if type(val) in [dict]:
                            for key2,val2 in val.iteritems():
                                result = '\n'.join([result,key+': '+str(val)]) #Improve, should print dicts right
                        
                        else:
                            result = '\n'.join([result,key+': '+str(val)])
                
                return result.lstrip().rstrip()

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
            pass

    def print_shodan(self):
        try:
            if self.shodan:

                result = ''.join(['IP: ',self.shodan.get('ip_str'),'\n'])
                result = ''.join([result,'Organization: ',self.shodan.get('org','n/a'),'\n'])

                if self.shodan.get('os','n/a'):
                    result = ''.join([result,'OS: ',self.shodan.get('os','n/a'),'\n'])

                if len(self.shodan['data']) > 0:
                    for item in self.shodan['data']:
                        result = ''.join([
                            
                            result,
                            'Port: {}'.format(item['port']),
                            '\n',
                            'Banner: {}'.format(item['data'].replace('\n','\n\t').rstrip()),

                            ])

                return result.rstrip().lstrip()

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)

class Scan(object):
    '''
    Object that will hold all Host entries, interpret uset given flags, manage scans, threads and outputs.


    Attributes are:
        feedback - Bool flag for output printing. Static variable.
        versobe - Bool flag for verbose output printing. Static variable.
        dns_server - Str DNS server to be used for lookups (consumed by dns.resolver module)
        shodan_key - Str key used for Shodan lookups
        targets - Set of Hosts that will be scanned
        bad_targets - Set of user inputs that could not be understood or resolved

    '''

    feedback = False
    verbose = False

    def __init__(self,dns_server=None,shodan_key=None,feedback=False,verbose=False):

        Scan.feedback = feedback
        Scan.verbose=verbose
        self.dns_server = dns_server
        self.shodan_key = shodan_key
        self.targets = set()
        self.bad_targets = set()

    @staticmethod
    def error(e, method_name=None):
        if Scan.feedback and Scan.verbose: 
            print '# Error:', str(e),'| method:',method_name


    def populate(self, user_supplied_list):
        for user_supplied in user_supplied_list:
            self.add_host(user_supplied)

        if self.feedback:
            if len(self.targets)<1:
                print '# No hosts to scan'
            else:
                print '# Scanning',str(len(self.targets))+'/'+str(len(user_supplied_list)),'hosts'

                if not self.shodan_key:
                    print '# No Shodan key provided'
                else:
                    print'# Shodan key provided -',self.shodan_key


    def add_host(self, user_supplied, from_net=False):
        
        #is it an IP?
        try:
            ip = ipa.ip_address(user_supplied.decode('unicode-escape'))
            if not (ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback):
                self.targets.add(Host(ips=[str(ip)]))
                return
            else:
                self.bad_targets.add(user_supplied)
                return
        except Exception as e:
            pass

        #is it a valid network range?
        try:
            net = ipa.ip_network(user_supplied.decode('unicode-escape'),strict=False)
            if net.prefixlen < 16:
                if Scan.feedback: print '[-] Error: Not a good idea to scan anything bigger than a /16 is it?', user_supplied
            else:
                for ip in net:
                    self.add_host(str(ip), True)
                return
                
        except Exception as e:
            pass

        #is it a valid DNS?
        try:
            pass
            domain = user_supplied
            ips = dns.resolver.query(user_supplied)
            self.targets.add(Host(domain=domain,ips=[str(ip) for ip in ips]))
            return
        except Exception as e:
            #If here so results from network won't be so verbose
            if not from_net:
                if Scan.feedback: print '[-] Error: Couldn\'t resolve or understand', user_supplied
            pass

        self.bad_targets.add(user_supplied)


    def scan_targets(self):
        
        fb = Scan.feedback
        if len(self.targets)>0:

            for host in self.targets:
                
                if fb: print '\n# ____________________ Scanning {} ____________________ #\n'.format(str(host))
            

            ###DNS and Whois lookups###
                if fb: print '# DNS lookups'
                host.dns_lookups()
                if fb:
                    if host.domain:
                        print ''
                        print '[*] Domain: '+host.domain
                    
                    #IPs and reverse domains
                    if host.ips: 
                        print ''
                        print '[*] IPs & reverse DNS: '
                        print host.print_all_ips()
                    
                    
                host.ns_dns_lookup()
                #NS records
                if host.ns and fb:
                    print ''
                    print '[*] NS records:'
                    print host.print_all_ns()


                host.mx_dns_lookup()
                #MX records
                if host.mx and fb:
                    print ''
                    print '[*] MX records:'
                    print host.print_all_mx()


                if fb: 
                    print ''
                    print '# Whois lookups'

                host.get_whois_domain()
                if host.whois_domain and fb:
                    print '' 
                    print '[*] Whois domain:'
                    print host.whois_domain

                host.get_all_whois_ip()
                if fb:
                    m = host.print_all_whois_ip()
                    if m:
                        print ''
                        print '[*] Whois IP:'
                        print m

            #Shodan lookup
                if self.shodan_key:
                    
                    if fb: 
                        print ''
                        print '# Querying Shodan for open ports'

                    host.get_all_shodan(self.shodan_key)

                    if fb:
                        m = host.print_all_shodan()
                        if m:
                            print '[*] Shodan:'
                            print m

            #Google subdomains lookup
                if host.domain:
                    if fb:
                        print '' 
                        print '# Querying Google for subdomains and Linkedin pages, this might take a while'
                    
                    host.google_lookups()
                    
                    if fb:
                        if host.linkedin_page:
                            print ''
                            print '[*] Possible LinkedIn page: '+host.linkedin_page

                        if host.subdomains:
                            print ''
                            print '[*] Subdomains:'+'\n'+host.print_subdomains()
                        else:
                            print '[-] Error: No subdomains found in Google. If you are scanning a lot, Google might be blocking your requests.'


    def scan_related_cidrs(self):
        #DNS lookups on entire CIDRs taken from host.get_whois_ip()
        fb = self.feedback
        
        if len(self.targets)>0:

            for host in self.targets:
        
                if host.cidrs:
                    
                    if fb:
                        print ''
                        print '# Reverse DNS lookup on range(s) {} (related to {})'.format(', '.join(host.cidrs),str(host))
                    
                    host.reverse_dns_lookup_on_related_cidrs(feedback=True)
                    
    def write_output_csv(self, filename=None):
        if filename:
            filename = os.path.expanduser(filename)
            fb = self.feedback

            if fb: 
                print '' 
                print '# Saving output csv file'

            
            output_as_lines = []

            for host in self.targets:
                try:
                    #Using generator to get one csv line at a time (one Host can yield multiple lines)
                    generator = host.print_as_csv_lines()
                    while True:
                        output_as_lines.append(generator.next())
                except StopIteration:
                    pass
                

            output_written = False
            while not output_written:
                
                try:
                    with open(filename, 'wb') as f:
                        writer = csv.writer(f)

                        for line in output_as_lines:
                            writer.writerow(line)

                        output_written = True
                
                except Exception as e:
                    error = '[-] Something went wrong, can\'t open output file. Press anything to try again.'
                    if self.verbose: error = ''.join([error,'\nError: ',str(e)])
                    raw_input(error)
                
                except KeyboardInterrupt:
                    if raw_input('[-] Sure you want to exit without saving your file ([y]/n)?') in ['y','Y','']:
                        sys.exit('# Scan interrupted')


    def exit(self):
        if self.feedback:
            print '# Done'
        

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='InstaRecon')
    parser.add_argument('targets', nargs='+', help='targets')
    parser.add_argument('-o', '--output', required=False,nargs='?',help='Output filename as csv.')
    parser.add_argument('-d', '--dns_server', required=False, nargs='?',help='Alternative DNS server to query.')
    parser.add_argument('-s', '--shodan_key', required=False,nargs='?',help='Shodan key for automated lookups. To get one, simply register on https://www.shodan.io/.')
    parser.add_argument('-v','--verbose', action='store_true',help='Verbose errors')
    args = parser.parse_args()


    targets = sorted(set(args.targets)) #removes duplicates

    scan = Scan(
        dns_server=args.dns_server,
        shodan_key=args.shodan_key,
        feedback=True,
        verbose=args.verbose,
        )
    
    try:

        scan.populate(targets)

        scan.scan_targets()
        
        #This method handles KeyboardInterrupt internally in Host.reverse_dns_lookup_on_related_cidrs
        scan.scan_related_cidrs() #reverse dns lookups on entire CIDRs related to original targets

        scan.write_output_csv(args.output)

        scan.exit()

    except KeyboardInterrupt:
        sys.exit('# Scan interrupted')
