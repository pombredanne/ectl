from __future__ import print_function
import ectl.rundeck
from ectl.rundeck import legacy
import datetime
import os
import sys
from ectl import pathutil
import copy
import urllib2
from ectl import xhash

# parameter types
GENERAL = 'GENERAL'
FILE = 'FILE'
DATETIME = 'DATETIME'

# ------------------------------------------
try:
    default_template_path = os.environ['MODELE_TEMPLATE_PATH'].split(os.pathsep)
except:
    default_template_path = ['.']

# Search for input files
try:
    default_file_path = os.environ['MODELE_FILE_PATH'].split(os.pathsep)
except Exception as e:
    default_file_path = ['.']
# ------------------------------------------


def download_file(sval, download_dir):
    # Try to download the file
    # http://stackoverflow.com/questions/22676/how-do-i-download-a-file-over-http-using-python
    file_name = os.path.join(download_dir, sval)
    tmp_file_name = file_name + '.tmp'
    url = 'http://portal.nccs.nasa.gov/GISS_modelE/modelE_input_data/' + sval

    try:
        with open(tmp_file_name, 'wb') as fout:
            u = urllib2.urlopen(url)
            meta = u.info()
            file_size = int(meta.getheaders("Content-Length")[0])
            print("Downloading: %s Bytes: %s" % (file_name, file_size))

            file_size_dl = 0
            block_sz = 8192
            while True:
                buffer = u.read(block_sz)
                if not buffer:
                    break

                file_size_dl += len(buffer)
                fout.write(buffer)
                status = r"%10d  [%3.2f%%]" % (file_size_dl, file_size_dl * 100. / file_size)
                status = status + chr(8)*(len(status)+1)
                print(status, end='')

        # Downloaded the file OK; now put in final place
        os.rename(tmp_file_name, file_name)

        return file_name
    except:
        try:
            print('Removing file %s' % tmp_file_name)
            os.remove(tmp_file_name)
        except:
            pass
        raise

def replace_date(dd, suffix, result):
    try:
        yeari = int(dd['YEAR'+suffix])
        monthi = int(dd['MONTH'+suffix])
        datei = int(dd['DATE'+suffix])
        houri = int(dd['HOUR'+suffix])
        dd[result] = datetime.datetime(yeari, monthi, datei, houri, 0, 0)
        del dd['YEAR'+suffix]
        del dd['MONTH'+suffix]
        del dd['DATE'+suffix]
        del dd['HOUR'+suffix]
    except KeyError:
        pass

# ----------------------------------------------------------
class Param(object):
    def __init__(self, pname, type, value, line=None):
        self.pname = pname
        self.type = type
        self.value = value
        self.lines = [line]    # Info on where this came from: line number, filename, raw line, etc.
        self.rval = None    # Resolved value (i.e. full pathname)

        if self.type == DATETIME:
            dt = self.value
            if not isinstance(dt, datetime.datetime):
                raise ValueError('Values of type DATETYPE must have Python type datetime.datetime')
            if (dt.minute!=0) or (dt.second !=0) or (dt.microsecond!=0):
                raise ValueError('Values of type DATETYPE must be on the hour.  Error in: {}'.format(dt))
            if (dt.tzinfo is not None):
                raise ValueError('Values of type DATETYPE cannot have a timezone.  Error in: {}'.format(dt))

    @property
    def line(self):
        return self.lines[-1]

    def __lt__(self, other):
        return self.pname < other.pname

    def __repr__(self):
        return repr((self.pname, self.type, self.value))

    def sname(self):
        return '.'.join(pname)

class Params(dict):
    def set(self, pname, value, type=None, line=None):
        # Determine the type of the parameter, if not set
        if type is None:
            if isinstance(value, datetime.date) or isinstance(value, datetime.datetime):
                type = DATETIME
            else:
                type = GENERAL

        try:
            param = self[pname]
            param.__init__(pname, type, value)
            param.lines.append(line)
        except KeyError:
            param = Param(pname, type, value, line=line)
            self[pname] = param
        if line is not None:
            line.param = param

        # Full pathnames are already resolved...
        if (type == FILE) and (len(os.path.split(value)[0]) > 0):
            param.rval = param.value

        return param

    def add_legacy(self, legacy):
        """Extract rundeck parametesr from a legacy rundeck."""
        ret = True
        for line in legacy.sections['Data input files'].parsed_lines():
            symbol,fname = line.parsed
            self.set(symbol, fname, type=FILE, line=line)

        for line in legacy.sections['Parameters'].parsed_lines():
            symbol,value = line.parsed
            self.set(symbol, value, type=GENERAL, line=line)

        # ------- Deal with the namelists

        # Split into a series of namelists, splitting on 'ISTART=...'
        inputz = legacy.sections['InputZ']
        inputzs = list()
        inputz_cur = list()
        for line in inputz.parsed_lines():
            for item in line.parsed:
                if item[0].upper() == 'ISTART':
                    if len(inputz_cur) > 0:
                        inputzs.append(dict(inputz_cur))
                    inputz_cur = list()
                inputz_cur.append((item[0].upper(), item[1]))
        inputzs.append(dict(inputz_cur))

        for inputz in inputzs:
            replace_date(inputz, 'I', 'START_TIME')
            replace_date(inputz, 'E', 'END_TIME')

        if len(inputzs) > 2:
            raise ValueError('At most one ISTART line is allowed')

        prefixes = ('INPUTZ', 'INPUTZ_cold')
        for prefix,inputz in zip(prefixes,inputzs):
            for symbol,value in inputz.items():
                type = DATETIME if isinstance(value, datetime.datetime) else GENERAL
                self.set((prefix,symbol), value, type=type)

    def resolve(self, file_path=default_file_path, download=False, download_dir=None):
        """Writes param.rval for params of type FILE"""
        good = True
        if download_dir is None:
            download_dir = file_path[0]

        for param in self.values():
            if (param.type == FILE) and (param.rval is None):
                try:
                    param.rval = pathutil.search_file(param.value, file_path)
                except IOError as e:
                    # Could not resolve path; download it
                    try:
                        param.rval = download_file(param.value, download_dir)
                    except KeyboardInterrupt as e2:
                        print(e2)
                        raise
                    except Exception as e2:
                        sys.stderr.write('{}: {}\n'.format(param.pname, e2))
                        good = False

                    ret = False    # Error condition
                    fname_full = None
        if not good:
            raise Exception('Problem downloading at least one file')

# ------------------------------------------------------
class Build(object):
    def __init__(self, modele_root=None):
        self.modele_root = modele_root
        self.sources = set()    # Object Modules
        self.components = dict()    # Directories of sources --> options

        self.defines = dict()    # Preprocessor Options

    def update_hash(self, hash):
        xhash.update(self.sources, hash)
        xhash.update(self.components, hash)
        xhash.update(self.defines, hash)


# Do not hash the source files because we don't REALLY know what we need.
# Instead, hash the whole modelE directory; and store the hash, giving user
# control of when to update.
#         # Hash the source files themselves...
#         print('modele_root', self.modele_root)
#         if self.modele_root is not None:
#             model = os.path.join(self.modele_root, 'model')
#             model_files = set(os.listdir(model))
# 
#             for src in self.sources:
#                 code = None
#                 for ext in ['.f', '.F90', '.F', '.f90']:
#                     srcF = src + ext
#                     if srcF in model_files:
#                         fname = os.path.join(model, srcF)
#                         with open(fname) as fin:
#                             code = fin.read()
#                         print('Hashing %s' % fname)
#                         xhash.update(code, hash)
#                         break
# 
#         # Hash source in the components...
#         for component in self.components:
#             subdir = os.path.join(model, component)
#             subdir_files = os.listdir(subdir)
#             for src in subdir_files:
#                 _,ext = os.path.splitext(src)
#                 if ext in ['.f', '.F90', '.F', '.f90']:
#         print(self.components)

    def add_legacy(self, legacy):
        for line in legacy.sections['Object Modules'].parsed_lines():
            for src in line.parsed:
                self.sources.add(src)

        for line in legacy.sections['Preprocessor Options'].parsed_lines():
            symbol,value = line.parsed
            self.defines[symbol] = value

        for line in legacy.sections['Components'].parsed_lines():
            for component in line.parsed:
                self.components[component] = None

        for line in legacy.sections['Component Options'].parsed_lines():
            component,options = line.parsed
            if component not in self.components:
                raise ValueError('Options found for non-existant component %s' % component)
            self.components[component] = dict(options)


def inputz_key(line):
    prefix = line[:5]
    if prefix == 'ISTAR':
        return '@@0'
    elif prefix == 'YEARI':
        return '@@1'
    elif prefix == 'YEARE':
        return '@@2'
    else:
        return line


class Rundeck(object):
    def __init__(self, modele_root=None):
        self.preamble = None
        self.params = Params()
        self.build = Build(modele_root=modele_root)

        # Lift functions from self.params
        self.set = self.params.set
        self.resolve = self.params.resolve

    def update_hash(self, hash):
        xhash.update(self.build, hash)

    def add_legacy(self, legacy):
        self.legacy = legacy
        self.preamble = legacy.sections['preamble']
        self.params.add_legacy(legacy)
        self.build.add_legacy(legacy)

    def __repr__(self):
        return '\n'.join(('============= Rundeck',
            '--------- Preamble', \
            repr(self.preamble),
            '--------- Params', \
            repr(self.params),
            '-------- Sources', \
            repr(self.build.sources),
            '-------- Components', \
            repr(self.build.components),
            '-------- Defines', \
            repr(self.build.defines)))


    def write(self, out, sections=None, comments=True):
        # Determine param associated with each line in original rundeck.
        # This allows us to make changes as needed.
    #    params_by_line = dict()
    #    for param in self.params.values():
    #        for line in param.lines:
    #            params_by_line[line] = param

        if sections is None:
            isections = set(section.isection for section in self.legacy.sections.values())
        else:
            isections = set(self.legacy.sections[section].isection for section in sections)

        ps = ParamSections(self)
        ps.inputz.sort(key=inputz_key)
        ps.inputz_cold.sort(key=inputz_key)

        parameters_is = self.legacy.sections['Parameters'].isection
        inputz_is = self.legacy.sections['InputZ'].isection

        inputz_written = False

        for line in self.legacy.lines:
            if line.isection not in isections:
                continue
            if comments:
                raw = line.raw
            else:
                raw = line.remove_comments()+'\n'

            if line.isection == parameters_is:
                # Find param that this line originally set.
                param = line.param


                # Header lines, etc.
                if param is None:
                    if comments:
                        out.write(raw)
                    continue

                # If this param has been deleted, don't write it out!
                if param.pname not in self.params:
                    out.write('!'+raw)
                    continue

                # Write the line that most recently set this param.
                last_line = param.lines[-1]
                if last_line is not None:
                    if comments:
                        out.write(last_line.raw)
                    else:
                        out.write(last_line.remove_comments()+'\n')
                else:
                    # There's no raw line; we must make something up
                    out.write('%s=%s\n' % (param.name, param.value))
            elif line.isection == inputz_is:
#                if line.parsed is None:
#                    out.write(raw)
#                    continue

                if inputz_written:
                    continue

                out.write('\n &INPUTZ\n ')
                out.write('\n '.join(ps.inputz))
                out.write('\n ')
                out.write('\n '.join(ps.inputz_cold))
                out.write('\n/\n')

                inputz_written = True
            else:
                out.write(raw)

# ----------------------------------------------------
def load(fname, modele_root=None, template_path=None):

    if modele_root is None:
        if template_path is None:
            template_path = default_template_path
    else:
        if template_path is None:
            template_path = [os.path.join(modele_root, 'templates')]


    # Resolve the rundeck filename
    fname = pathutil.search_file(fname, template_path)

    # Add the directory of the rundeck to the path
    dirname,leafname = os.path.split(fname)
    template_path = [dirname] + template_path

    # Create a blank rundeck
    rd = Rundeck(modele_root=modele_root)

    root,ext = os.path.splitext(leafname)
    fin = legacy.preprocessor(fname, template_path)
    legacy_rundeck = legacy.LegacyRundeck(fin)    # Auto-closes
    rd.add_legacy(legacy_rundeck)


    return rd
# ----------------------------------------------------
def namelist_time(suffix, dt):
    return 'YEAR{0}={1},MONTH{0}={2},DATE{0}={3},HOUR{0}={4},' \
        .format(suffix,dt.year,dt.month,dt.day,dt.hour)


class ParamSections(object):
    def __init__(self, rd):
        """params: rd.params"""

        # output line sections
        self.parameters = []
        self.data_files = []
        self.data_lines = []
        self.inputz = []
        self.inputz_cold = []

        # Organize self.parameters into ModelE sections
        for param in sorted(list(rd.params.values())):
            pname = param.pname
            if isinstance(pname, str):    # Non-compound name
                if (id(param.type) == id(ectl.rundeck.FILE)):
                    if param.rval is not None:
                        self.data_lines.append(" _file_{}='{}'".format(pname, param.rval))
                        self.data_files.append((pname, param.rval))
    #                else:
    #                    self.parameters.append("! Not Found: {}={}".format(pname, param.sval))

                elif (id(param.type) == id(ectl.rundeck.GENERAL)):
                    self.parameters.append(' %s=%s' % (param.pname, param.value))
                elif (id(param.type) == id(ectl.rundeck.DATETIME)):
                    raise ValueError('Cannot put DATETIME values into self.parameters section of rundeck.')
                else:
                    raise ValueError('Unknown parameter type %s' % param.type)
            elif len(pname) == 2:
                if pname[0].lower() == 'inputz':
                    iz = self.inputz
                elif pname[0].lower() == 'inputz_cold':
                    iz = self.inputz_cold
                else:
                    raise ValueError('Unknown compound name: {}'.format(pname))

                if pname[1].upper() == 'END_TIME':
                    iz.append(namelist_time('E', param.value))
                elif pname[1].upper() == 'START_TIME':
                    iz.append(namelist_time('I', param.value))
                else:
                    line = '{}={},'.format(pname[1],param.value)
                    iz.append(line)



