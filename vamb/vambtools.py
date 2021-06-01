__doc__ = "Various classes and functions Vamb uses internally."

import os as _os
import gzip as _gzip
import bz2 as _bz2
import lzma as _lzma
import numpy as _np
from vamb._vambtools import _kmercounts, _overwrite_matrix
import collections as _collections
from hashlib import md5 as _md5
from typing import Optional, Dict, Set, Iterable, Iterator, Union, Tuple, Collection, TextIO

class PushArray:
    """Data structure that allows efficient appending and extending a 1D Numpy array.
    Intended to strike a balance between not resizing too often (which is slow), and
    not allocating too much at a time (which is memory inefficient).

    Usage:
    >>> arr = PushArray(numpy.float64)
    >>> arr.append(5.0)
    >>> arr.extend(numpy.linspace(4, 3, 3))
    >>> arr.take() # return underlying Numpy array
    array([5. , 4. , 3.5, 3. ])
    """

    __slots__ = ['data', 'capacity', 'length']

    def __init__(self, dtype, start_capacity: int=1<<16):
        self.capacity = start_capacity
        self.data = _np.empty(self.capacity, dtype=dtype)
        self.length = 0

    def __len__(self) -> int:
        return self.length

    def _setcapacity(self, n: int):
        self.data.resize(n, refcheck=False)
        self.capacity = n

    def _grow(self, mingrowth: int):
        """Grow capacity by power of two between 1/8 and 1/4 of current capacity, though at
        least mingrowth"""
        growth = max(int(self.capacity * 0.125), mingrowth)
        nextpow2 = 1 << (growth - 1).bit_length()
        self._setcapacity(self.capacity + nextpow2)

    def append(self, value):
        if self.length == self.capacity:
            self._grow(64)

        self.data[self.length] = value
        self.length += 1

    def extend(self, values):
        lenv = len(values)
        if self.length + lenv > self.capacity:
            self._grow(lenv)

        self.data[self.length:self.length+lenv] = values
        self.length += lenv

    def take(self) -> _np.ndarray:
        "Return the underlying array"
        self._setcapacity(self.length)
        return self.data

    def clear(self, force: bool=False):
        "Empties the PushArray. If force is true, also truncates the underlying memory."
        self.length = 0
        if force:
            self._setcapacity(0)

def zscore(array : _np.ndarray, axis: Optional[int]=None, inplace: bool=False) -> _np.ndarray:
    """Calculates zscore for an array. A cheap copy of scipy.stats.zscore.

    Inputs:
        array: Numpy array to be normalized
        axis: Axis to operate across [None = entrie array]
        inplace: Do not create new array, change input array [False]

    Output:
        If inplace is True: Input numpy array
        else: New normalized Numpy-array
    """

    if axis is not None and axis >= array.ndim:
        raise _np.AxisError(axis)

    if inplace and not _np.issubdtype(array.dtype, _np.floating):
        raise TypeError('Cannot convert a non-float array to zscores')

    mean = array.mean(axis=axis)
    std = array.std(axis=axis)

    if axis is None:
        if std == 0:
            std = 1 # prevent divide by zero

    else:
        std[std == 0.0] = 1 # prevent divide by zero
        shape = tuple(dim if ax != axis else 1 for ax, dim in enumerate(array.shape))
        mean.shape, std.shape = shape, shape

    if inplace:
        array -= mean
        array /= std
        return array
    else:
        return (array - mean) / std

def numpy_inplace_maskarray(array: _np.ndarray, mask: _np.ndarray) -> _np.ndarray:
    """In-place masking of a Numpy array, i.e. if `mask` is a boolean mask of same
    length as `array`, then array[mask] == numpy_inplace_maskarray(array, mask),
    but does not allocate a new array.
    """

    if len(mask) != len(array):
        raise ValueError('Lengths of array and mask must match')
    elif len(array.shape) != 2:
        raise ValueError('Can only take a 2 dimensional-array.')

    uints = _np.frombuffer(mask, dtype=_np.uint8)
    index = _overwrite_matrix(array, uints)
    array.resize((index, array.shape[1]), refcheck=False)
    return array

def torch_inplace_maskarray(array, mask):
    """In-place masking of a Tensor, i.e. if `mask` is a boolean mask of same
    length as `array`, then array[mask] == torch_inplace_maskarray(array, mask),
    but does not allocate a new tensor.
    """

    if len(mask) != len(array):
        raise ValueError('Lengths of array and mask must match')
    elif array.dim() != 2:
        raise ValueError('Can only take a 2 dimensional-array.')

    np_array = array.numpy()
    np_mask = _np.frombuffer(mask.numpy(), dtype=_np.uint8)
    index = _overwrite_matrix(np_array, np_mask)
    array.resize_((index, array.shape[1]))
    return array

class Reader:
    """Use this instead of `open` to open files which are either plain text,
    gzipped, bzip2'd or zipped with LZMA.

    Usage:
    >>> with Reader(file, readmode) as file: # by default textmode
    >>>     print(next(file))
    TEST LINE
    """

    def __init__(self, filename: str, readmode: str='r'):
        if readmode not in ('r', 'rt', 'rb'):
            raise ValueError("the Reader cannot write, set mode to 'r' or 'rb'")
        if readmode == 'r':
            self.readmode = 'rt'
        else:
            self.readmode = readmode

        self.filename = filename

        with open(self.filename, 'rb') as f:
            signature = f.peek(8)[:8]

        # Gzipped files begin with the two bytes 0x1F8B
        if tuple(signature[:2]) == (0x1F, 0x8B):
            self.filehandle = _gzip.open(self.filename, self.readmode)

        # bzip2 files begin with the signature BZ
        elif signature[:2] == b'BZ':
            self.filehandle = _bz2.open(self.filename, self.readmode)

        # .XZ files begins with 0xFD377A585A0000
        elif tuple(signature[:7]) == (0xFD, 0x37, 0x7A, 0x58, 0x5A, 0x00, 0x00):
            self.filehandle = _lzma.open(self.filename, self.readmode)

        # Else we assume it's a text file.
        else:
            self.filehandle = open(self.filename, self.readmode)

    def close(self):
        self.filehandle.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def __iter__(self):
        return self.filehandle

class FastaEntry:
    """One single FASTA entry. Instantiate with string header and bytearray
    sequence."""

    basemask = bytearray.maketrans(b'acgtuUswkmyrbdhvnSWKMYRBDHV',
                                   b'ACGTTTNNNNNNNNNNNNNNNNNNNNN')
    __slots__ = ['header', 'sequence']

    def __init__(self, header: str, sequence: bytearray):
        if len(header) > 0 and (header[0] in ('>', '#') or header[0].isspace()):
            raise ValueError('Header cannot begin with #, > or whitespace')
        if '\t' in header:
            raise ValueError('Header cannot contain a tab')

        masked = sequence.translate(self.basemask, b' \t\n\r')
        stripped = masked.translate(None, b'ACGTN')
        if len(stripped) > 0:
            bad_character = chr(stripped[0])
            raise ValueError(f"Non-IUPAC DNA byte in sequence {header}: '{bad_character}'")

        self.header = header
        self.sequence = masked

    def __len__(self) -> int:
        return len(self.sequence)

    def __str__(self) -> str:
        return f'>{self.header}\n{self.sequence.decode()}'

    def format(self, width: int=60) -> str:
        sixtymers = range(0, len(self.sequence), width)
        spacedseq = '\n'.join([self.sequence[i: i+width].decode() for i in sixtymers])
        return f'>{self.header}\n{spacedseq}'

    def __getitem__(self, index):
        return self.sequence[index]

    def __repr__(self) -> str:
        return f'<FastaEntry {self.header}>'

    def kmercounts(self, k: int) -> _np.ndarray:
        if k < 1 or k > 10:
            raise ValueError('k must be between 1 and 10 inclusive')

        counts = _np.zeros(1 << (2*k), dtype=_np.int32)
        _kmercounts(self.sequence, k, counts)
        return counts

def byte_iterfasta(filehandle: Iterable[bytes], comment: bytes=b'#'):
    """Yields FastaEntries from a binary opened fasta file.

    Usage:
    >>> with Reader('/dir/fasta.fna', 'rb') as filehandle:
    ...     entries = byte_iterfasta(filehandle) # a generator

    Inputs:
        filehandle: Any iterator of binary lines of a FASTA file
        comment: Ignore lines beginning with any whitespace + comment

    Output: Generator of FastaEntry-objects from file
    """

    # Make it work for persistent iterators, e.g. lists
    line_iterator = iter(filehandle)
    # Skip to first header
    try:
        for probeline in line_iterator:
            stripped = probeline.lstrip()
            if stripped.startswith(comment):
                pass

            elif probeline[0:1] == b'>':
                break

            else:
                raise ValueError('First non-comment line is not a Fasta header')

        else: # no break
            return None
            #raise ValueError('Empty or outcommented file')

    except TypeError:
        errormsg = 'First line does not contain bytes. Are you reading file in binary mode?'
        raise TypeError(errormsg) from None

    header = probeline[1:-1].decode()
    buffer = list()

    # Iterate over lines
    for line in line_iterator:
        if line.startswith(comment):
            pass

        elif line.startswith(b'>'):
            yield FastaEntry(header, bytearray().join(buffer))
            buffer.clear()
            header = line[1:-1].decode()

        else:
            buffer.append(line)

    yield FastaEntry(header, bytearray().join(buffer))

def write_clusters(
    filehandle: TextIO,
    clusters: Union[Dict, Iterable[Tuple[str, Collection]]],
    max_clusters:Optional[int]=None,
    min_size: int=1,
    header: Optional[str]=None,
    rename: bool=True
) -> Tuple[int, int]:
    """Writes clusters to an open filehandle.
    Inputs:
        filehandle: An open filehandle that can be written to
        clusters: An iterator generated by function `clusters` or a dict
        max_clusters: Stop printing after this many clusters [None]
        min_size: Don't output clusters smaller than N contigs
        header: Commented one-line header to add
        rename: Rename clusters to "cluster_1", "cluster_2" etc.

    Outputs:
        clusternumber: Number of clusters written
        ncontigs: Number of contigs written
    """

    if not hasattr(filehandle, 'writable') or not filehandle.writable():
        raise ValueError('Filehandle must be a writable file')

    # Special case to allows dicts even though they are not iterators of
    # clustername, {cluster}
    if isinstance(clusters, dict):
        clusters = clusters.items()

    if max_clusters is not None and max_clusters < 1:
        raise ValueError('max_clusters must None or at least 1, not {max_clusters}')

    if header is not None and len(header) > 0:
        if '\n' in header:
            raise ValueError('Header cannot contain newline')

        if header[0] != '#':
            header = '# ' + header

        print(header, file=filehandle)

    clusternumber = 0
    ncontigs = 0

    for clustername, contigs in clusters:
        if len(contigs) < min_size:
            continue

        if rename:
            clustername = 'cluster_' + str(clusternumber + 1)

        for contig in contigs:
            print(clustername, contig, sep='\t', file=filehandle)
        filehandle.flush()

        clusternumber += 1
        ncontigs += len(contigs)

        if clusternumber == max_clusters:
            break

    return clusternumber, ncontigs

def read_clusters(filehandle: Iterable[str], min_size: int=1) -> Dict[str, Set[str]]:
    """Read clusters from a file as created by function `writeclusters`.

    Inputs:
        filehandle: An open filehandle that can be read from
        min_size: Minimum number of contigs in cluster to be kept

    Output: A {clustername: set(contigs)} dict"""

    contigsof = _collections.defaultdict(set)

    for line in filehandle:
        stripped = line.strip()

        if not stripped or stripped[0] == '#':
            continue

        clustername, contigname = stripped.split('\t')
        contigsof[clustername].add(contigname)

    contigsof = {cl: co for cl, co in contigsof.items() if len(co) >= min_size}

    return contigsof


def loadfasta(
    byte_iterator: Iterable[bytes],
    keep: Optional[Collection]=None,
    comment: bytes=b'#',
    compress: bool=False
):
    """Loads a FASTA file into a dictionary.

    Usage:
    >>> with Reader('/dir/fasta.fna', 'rb') as filehandle:
    ...     fastadict = loadfasta(filehandle)

    Input:
        byte_iterator: Iterator of binary lines of FASTA file
        keep: Keep entries with headers in `keep`. If None, keep all entries
        comment: Ignore lines beginning with any whitespace + comment
        compress: Keep sequences compressed [False]

    Output: {header: FastaEntry} dict
    """

    entries = dict()

    for entry in byte_iterfasta(byte_iterator, comment=comment):
        if keep is None or entry.header in keep:
            if compress:
                entry.sequence = bytearray(_gzip.compress(entry.sequence, compresslevel=2))

            entries[entry.header] = entry

    return entries

def write_bins(
    directory,
    bins: Dict[str, Set[str]],
    fastadict: Dict[str, FastaEntry],
    compressed: bool=False,
    maxbins: Optional[int]=250,
    minsize: int=0
):
    """Writes bins as FASTA files in a directory, one file per bin.

    Inputs:
        directory: Directory to create or put files in
        bins: {'name': {set of contignames}} dictionary (can be loaded from
        clusters.tsv using vamb.cluster.read_clusters)
        fastadict: {contigname: FastaEntry} dict as made by `loadfasta`
        compressed: Sequences in dict are compressed [False]
        maxbins: None or else raise an error if trying to make more bins than this [250]
        minsize: Minimum number of nucleotides in cluster to be output [0]

    Output: None
    """

    # Safety measure so someone doesn't accidentally make 50000 tiny bins
    # If you do this on a compute cluster it can grind the entire cluster to
    # a halt and piss people off like you wouldn't believe.
    if maxbins is not None and len(bins) > maxbins:
        raise ValueError(f'{len(bins)} bins exceed maxbins of {maxbins}')

    # Check that the directory is not a non-directory file,
    # and that its parent directory indeed exists
    abspath = _os.path.abspath(directory)
    parentdir = _os.path.dirname(abspath)

    if parentdir != '' and not _os.path.isdir(parentdir):
        raise NotADirectoryError(parentdir)

    if _os.path.isfile(abspath):
        raise NotADirectoryError(abspath)

    if minsize < 0:
        raise ValueError("Minsize must be nonnegative")

    # Check that all contigs in all bins are in the fastadict
    allcontigs = set()

    for contigs in bins.values():
        allcontigs.update(set(contigs))

    allcontigs -= fastadict.keys()
    if allcontigs:
        nmissing = len(allcontigs)
        raise IndexError(f'{nmissing} contigs in bins missing from fastadict')

    # Make the directory if it does not exist - if it does, do nothing
    try:
        _os.mkdir(directory)
    except FileExistsError:
        pass
    except:
        raise

    # Now actually print all the contigs to files
    for binname, contigs in bins.items():
        # Load bin into a list, decompress that bin if necessary
        bin = []
        for contig in contigs:
            entry = fastadict[contig]
            if compressed:
                uncompressed = bytearray(_gzip.decompress(entry.sequence))
                entry = FastaEntry(entry.header, uncompressed)
            bin.append(entry)

        # Skip bin if it's too small
        if minsize > 0 and sum(len(entry) for entry in bin) < minsize:
            continue

        # Print bin to file
        filename = _os.path.join(directory, binname + '.fna')
        with open(filename, 'w') as file:
            for entry in bin:
                print(entry.format(), file=file)

def validate_input_array(array: _np.ndarray) -> _np.ndarray:
    "Returns array similar to input array but C-contiguous and with own data."
    if not array.flags['C_CONTIGUOUS']:
        array = _np.ascontiguousarray(array)
    if not array.flags['OWNDATA']:
        array = array.copy()

    assert (array.flags['C_CONTIGUOUS'] and array.flags['OWNDATA'])
    return array

def read_npz(file) -> _np.ndarray:
    """Loads array in .npz-format

    Input: Open file or path to file with npz-formatted array

    Output: A Numpy array
    """

    npz = _np.load(file)
    array = validate_input_array(npz['arr_0'])
    npz.close()

    return array

def write_npz(file, array: _np.ndarray):
    """Writes a Numpy array to an open file or path in .npz format

    Inputs:
        file: Open file or path to file
        array: Numpy array

    Output: None
    """
    _np.savez_compressed(file, array)

def filtercontigs(infile: Iterable[bytes], outfile: TextIO, minlength: int=2000):
    """Creates new FASTA file with filtered contigs

    Inputs:
        infile: Binary opened input FASTA file
        outfile: Write-opened output FASTA file
        minlength: Minimum contig length to keep [2000]

    Output: None
    """

    fasta_entries = byte_iterfasta(infile)

    for entry in fasta_entries:
        if len(entry) > minlength:
            print(entry.format(), file=outfile)

def concatenate_fasta(outfile: TextIO, inpaths: Iterable[str], minlength: int=2000, rename: bool=True):
    """Creates a new FASTA file from input paths, and optionally rename contig headers
    to the pattern "S{sample number}C{contig identifier}".

    Inputs:
        outpath: Open filehandle for output file
        inpaths: Iterable of paths to FASTA files to read from
        minlength: Minimum contig length to keep [2000]
        rename: Rename headers

    Output: None
    """

    headers = set()
    for (inpathno, inpath) in enumerate(inpaths):
        with Reader(inpath, "rb") as infile:

            # If we rename, seq headers only have to be unique for each sample
            if rename:
                headers.clear()
            entries = byte_iterfasta(infile)

            for entry in entries:
                if len(entry) < minlength:
                    continue

                header = entry.header
                identifier = header.split()[0]

                if rename:
                    newheader = f"S{inpathno + 1}C{identifier}"
                else:
                    newheader = identifier

                if newheader in headers:
                    raise ValueError("Multiple sequences would be given "
                                     f"header {newheader}.")
                headers.add(newheader)

                entry.header = newheader
                print(entry.format(), file=outfile)

def hash_refnames(refnames: Iterable[str]) -> bytes:
    "Hashes an iterable of strings of reference names using MD5."
    hasher = _md5()
    for refname in refnames:
        hasher.update(refname.encode().rstrip())

    return hasher.digest()

def verify_refhash(refnames: Iterable[str], expected: bytes) -> None:
    "Compares hash of refnames with bytes expected and errors if not the same"    
    refhash = hash_refnames(refnames)
    if refhash != expected:
        raise ValueError(
            f"Got reference name hash {refhash.hex()}, expected {expected.hex()}. "
            "Make sure all BAM and FASTA headers are identical "
            "and in the same order."
        )

def load_jgi(filehandle: Iterator[str], minlength: int, refhash: Optional[bytes]):
    """Load depths from the --outputDepth of jgi_summarize_bam_contig_depths.
    See https://bitbucket.org/berkeleylab/metabat for more info on that program.

    Usage:
        with open('/path/to/jgi_depths.tsv') as file:
            depths = load_jgi(file, 1000, my_hash)
    Input:
        filehandle: File handle of open output depth file
        minlength: Minimum contig length to keep
        refhash: Hash of references to compare against (None = no check)
    Output:
        N_contigs x N_samples Numpy matrix of dtype float32
    """
    header = next(filehandle)
    fields = header.strip().split('\t')
    if not fields[:3] == ["contigName", "contigLen", "totalAvgDepth"]:
        raise ValueError('Input file format error: First columns should be "contigName,"'
        '"contigLen" and "totalAvgDepth"')

    columns = tuple([i for i in range(3, len(fields)) if not fields[i].endswith("-var")])
    array = PushArray(_np.float32)
    identifiers = list()

    for row in filehandle:
        fields = row.split('\t')
        # We use float because very large numbers will be printed in scientific notation
        if float(fields[1]) < minlength:
            continue

        for col in columns:
            array.append(float(fields[col]))
        
        identifiers.append(fields[0])

    if refhash is not None:
        verify_refhash(identifiers, refhash)

    result = array.take()
    result.shape = (len(result) // len(columns), len(columns))
    return validate_input_array(result)

def _split_bin(binname, headers, separator: str, bysample=_collections.defaultdict(set)):
    "Split a single bin by the prefix of the headers"

    bysample.clear()
    for header in headers:
        if not isinstance(header, str):
            raise TypeError(f'Can only split named sequences, not of type {type(header)}')

        sample, _, identifier = header.partition(separator)

        if not identifier:
            raise KeyError(f"Separator '{separator}' not in sequence label: '{header}'")

        bysample[sample].add(header)

    for sample, splitheaders in bysample.items():
        newbinname = f"{sample}{separator}{binname}"
        yield newbinname, splitheaders

def _binsplit_generator(cluster_iterator: Iterable[Tuple[str, Iterable[str]]], separator: str):
    "Return a generator over split bins with the function above."
    for binname, headers in cluster_iterator:
        for newbinname, splitheaders in _split_bin(binname, headers, separator):
            yield newbinname, splitheaders

def binsplit(clusters: Union[Dict[str, Set[str]], Iterable[Tuple[str, Iterable[str]]]], separator: str):
    """Splits a set of clusters by the prefix of their names.
    The separator is a string which separated prefix from postfix of contignames. The
    resulting split clusters have the prefix and separator prepended to them.

    clusters can be an iterator, in which case this function returns an iterator, or a dict
    with contignames: set_of_contignames pair, in which case a dict is returned.

    Example:
    >>> clusters = {"bin1": {"s1-c1", "s1-c5", "s2-c1", "s2-c3", "s5-c8"}}
    >>> binsplit(clusters, "-")
    {'s2-bin1': {'s1-c1', 's1-c3'}, 's1-bin1': {'s1-c1', 's1-c5'}, 's5-bin1': {'s1-c8'}}
    """

    if isinstance(clusters, dict):
        return dict(_binsplit_generator(clusters.items(), separator))
    else:
        return _binsplit_generator(clusters, separator)
